"""Pre-registered `haa` candidate sleeve: Keller & Keuning (2022) Hybrid Asset
Allocation, balanced variant, on daily-bar ETFs.

Registration: `reports/absolute_return_campaign_registration.json`
(`candidate_sleeves.haa`). Universe, canary, momentum definition, cadence
and the pass rule all come from there; nothing here adds a grid point.

Rules
-----
- Offensive universe: SPY, IWM, EFA, EEM, VNQ, DBC, IEF, TLT. Canary: TIP.
  Defensive: the better of BIL and IEF by momentum (BIL is cash before its
  2007-05-30 inception).
- Momentum score = mean of the 1-, 3-, 6- and 12-month total returns, taken
  as 21 / 63 / 126 / 252 trading-day lookbacks on adjusted close (the paper
  uses calendar months; trading-day windows are the equivalent on a daily
  panel and avoid month-boundary alignment artefacts).
- Rebalance monthly on the last trading day of each month using closes
  through that day; the new weights apply from the NEXT trading day
  (`target_weights` writes them on the first day of the following month),
  so nothing is traded on information from the same close.
- If the canary's momentum is > 0: hold the top-`top_k` (4) offensive
  assets by momentum at equal weight (1/top_k each), except that any of the
  four whose own momentum is <= 0 has its slot placed in the defensive pick
  instead. If the canary's momentum is <= 0: 100% in the defensive pick.

Standalone vs in a mix
----------------------
`target_weights` returns the sleeve at weight 1.0 (rows sum to 1.0 once
every instrument has a price history; earlier rows may sum to less because
an unlisted instrument is cash). The mix study scales it.

Benchmark
---------
Raw SPY close-to-close total return (Tiingo adjusted close, zero cost) —
the registration benchmark. The like-for-like simulated-SPY row is a
diagnostic, not judged.

Decision rule (declared before any result was seen)
---------------------------------------------------
`sleeve_screened_for_mixes` if the sleeve passes
`passes_gate(objective_class="benchmark_beater")` in every one of
full + heldout_2023_plus + early_2020_2022 at 8 bps against the
registration benchmark AND its GFC max drawdown is inside the paired band
(`fails_stress` false). HAA was published in 2022, so per the
registration's post-publication filter the heldout_2023_plus cell is the
only out-of-sample evidence and is reported on its own. The mix study, not
this file, makes the promotion call.

Reproduce
---------
    .venv/bin/python -m backtest.haa_study        # < 1 minute
    .venv/bin/python -m pytest tests/test_haa_study.py -q
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
    load_close as _load_history,
)

REGISTRATION = "reports/absolute_return_campaign_registration.json"
REPORT = Path("reports/haa_study.json")
OBJECTIVE_CLASS = "benchmark_beater"

OFFENSIVE = ["SPY", "IWM", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT"]
CANARY = "TIP"
DEFENSIVE = ["BIL", "IEF"]
INSTRUMENTS = sorted(set(OFFENSIVE) | {CANARY} | set(DEFENSIVE))
BENCHMARK = "SPY"
LOOKBACKS = (21, 63, 126, 252)
TOP_K = 4

EQUITY = 10_000.0
ELEVATED_CAP_PCT = 0.80
POSITION_CAP_PCT = 0.15
COST_BPS = 8.0
COST_BPS_STRESS = 20.0
REQUIRED_CELLS = ("full", "heldout_2023_plus", "early_2020_2022")


def load_close(end: str = windows.HELDOUT[1]) -> pd.DataFrame:
    """All HAA instruments, tz-naive adjusted closes, `<= end` (never past
    the frozen boundary)."""
    return _load_history(INSTRUMENTS, end=end)


def momentum_score(close: pd.DataFrame, lookbacks=LOOKBACKS) -> pd.DataFrame:
    """Mean of the 1/3/6/12-month total returns; NaN until every lookback
    has data for that symbol."""
    parts = [close / close.shift(n) - 1.0 for n in lookbacks]
    return sum(parts) / len(parts)


def month_end_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each calendar month present in `index`."""
    frame = pd.Series(index, index=index)
    return pd.DatetimeIndex(frame.groupby([index.year, index.month]).max().to_numpy())


def allocation_for(score: pd.Series, *, top_k: int = TOP_K) -> dict[str, float]:
    """Weights decided from one day's momentum scores (the paper's rule)."""
    defensive_scores = score.reindex(DEFENSIVE).dropna()
    # BIL before inception (NaN) cannot be chosen; if neither defensive asset
    # has a score yet the defensive slot is cash.
    defensive = defensive_scores.idxmax() if len(defensive_scores) else None
    weights: dict[str, float] = {}

    def add(symbol: str | None, w: float) -> None:
        if symbol is None:
            return
        weights[symbol] = weights.get(symbol, 0.0) + w

    canary = score.get(CANARY, np.nan)
    if not (canary > 0):
        add(defensive, 1.0)
        return weights
    offensive = score.reindex(OFFENSIVE).dropna().sort_values(ascending=False)
    chosen = list(offensive.head(top_k).index)
    slot = 1.0 / top_k
    for symbol in chosen:
        if offensive[symbol] > 0:
            add(symbol, slot)
        else:
            add(defensive, slot)
    return weights


def target_weights(close: pd.DataFrame, *, top_k: int = TOP_K) -> pd.DataFrame:
    """Daily target weights, sleeve weight 1.0. Decided on each month's
    last trading day from closes through that day, applied from the next
    trading day."""
    score = momentum_score(close)
    decision_days = month_end_days(close.index)
    weights = pd.DataFrame(0.0, index=close.index, columns=INSTRUMENTS)
    positions = np.arange(len(close.index))
    for day in decision_days:
        loc = close.index.get_loc(day)
        if loc + 1 >= len(close.index):
            break  # decided on the last day of the panel; nothing to apply
        alloc = allocation_for(score.iloc[loc], top_k=top_k)
        start = loc + 1
        later = decision_days[decision_days > day]
        end = close.index.get_loc(later[0]) + 1 if len(later) else len(close.index)
        for symbol, w in alloc.items():
            weights.iloc[start:end, weights.columns.get_loc(symbol)] = w
    del positions
    return weights


# --------------------------------------------------------------------------
# Simulation and judging
# --------------------------------------------------------------------------


def simulate_sleeve(weights: pd.DataFrame, close: pd.DataFrame, *, cost_bps: float = COST_BPS):
    return simulate_targets(
        weights, close[weights.columns], equity=EQUITY, gross_leverage=1.0,
        position_cap_pct=POSITION_CAP_PCT, elevated_cap=(set(INSTRUMENTS), ELEVATED_CAP_PCT),
        leveraged_cap_pct=1.0, cost_bps_by_symbol=cost_bps,
    )


def spy_like_for_like(close: pd.DataFrame, start, *, cost_bps: float = COST_BPS) -> pd.Series:
    frame = close.loc[close.index >= start, [BENCHMARK]]
    weights = pd.DataFrame({BENCHMARK: 1.0}, index=frame.index)
    returns, _ = simulate_targets(
        weights, frame, equity=EQUITY, gross_leverage=1.0,
        elevated_cap=({BENCHMARK}, ELEVATED_CAP_PCT), leveraged_cap_pct=1.0,
        cost_bps_by_symbol=cost_bps,
    )
    return returns


def cell_windows(first_live: pd.Timestamp, end: pd.Timestamp) -> dict:
    cells: dict = {"full": (first_live.date().isoformat(), end.date().isoformat())}
    for label, bounds in windows.SCREEN_WINDOWS.items():
        cells[label] = bounds if first_live <= pd.Timestamp(bounds[0]) else None
    for label, bounds in windows.STRESS_WINDOWS.items():
        cells[f"stress:{label}"] = bounds if first_live <= pd.Timestamp(bounds[0]) else None
    return cells


def diagnostics(weights: pd.DataFrame, returns: pd.Series, diag: pd.DataFrame, spy_raw: pd.Series,
                first_live: pd.Timestamp) -> dict:
    live = weights.loc[weights.index >= first_live]
    r = returns.loc[returns.index >= first_live]
    defensive_w = live[DEFENSIVE].sum(axis=1)
    month_ends = month_end_days(live.index)
    monthly_def = defensive_w.reindex(month_ends)
    turnover = diag.loc[diag.index >= first_live, "turnover"]
    b = spy_raw.reindex(r.index).fillna(0.0)
    years = r.index.year
    cov = np.cov(r.to_numpy(), b.to_numpy())
    return {
        "months_fully_defensive_pct": round(float((monthly_def >= 0.999).mean()), 4),
        "avg_defensive_weight": round(float(defensive_w.mean()), 4),
        "annual_turnover_one_way": round(float(turnover.groupby(turnover.index.year).sum().mean()), 2),
        "realized_vol_annualized": round(float(r.std() * np.sqrt(252)), 4),
        "beta_to_spy": round(float(cov[0, 1] / cov[1, 1]), 3) if cov[1, 1] > 0 else None,
        "calendar_years": {
            str(y): {"sleeve": round(float((1 + r[years == y]).prod() - 1), 4),
                     "spy": round(float((1 + b[years == y]).prod() - 1), 4)}
            for y in sorted(set(years))
        },
        "avg_offensive_holdings": round(float((live[OFFENSIVE] > 0).sum(axis=1).mean()), 2),
    }


def tsmom_comparison(close_end: pd.Timestamp, rf: pd.Series, spy_raw: pd.Series, haa_returns: pd.Series) -> dict:
    """The shipped tsmom sleeve's own stream (production construction) over
    HAA's live dates, since HAA is a candidate for tsmom's diversifier role."""
    try:
        from backtest.production_portfolio import build_streams
        streams = build_streams()
        tsmom = streams["tsmom"] if "tsmom" in streams else None
    except Exception as exc:  # pragma: no cover - diagnostic only
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if tsmom is None:
        return {"available": False, "reason": "build_streams() returned no 'tsmom' stream"}
    tsmom = tsmom.copy()
    tsmom.index = pd.DatetimeIndex(tsmom.index)
    if tsmom.index.tz is not None:
        tsmom.index = tsmom.index.tz_convert("UTC").tz_localize(None)
    tsmom.index = tsmom.index.normalize()
    tsmom = tsmom.loc[tsmom.index <= close_end]
    out = {"available": True, "construction": "backtest.production_portfolio.build_streams()['tsmom'] (15 assets, 252d sign, inverse vol, 8 bps)"}
    for label, bounds in (("full_haa_period", (haa_returns.index[0].date().isoformat(), close_end.date().isoformat())),
                          ("early_2020_2022", windows.SCREEN_WINDOWS["early_2020_2022"]),
                          ("heldout_2023_plus", windows.SCREEN_WINDOWS["heldout_2023_plus"])):
        t = windows.slice_window(tsmom, bounds).dropna()
        h = windows.slice_window(haa_returns, bounds).dropna()
        idx = t.index.intersection(h.index)
        if len(idx) < 63:
            out[label] = None
            continue
        out[label] = {
            "tsmom": returns_summary(t.loc[idx], "tsmom", rf=rf),
            "haa": returns_summary(h.loc[idx], "haa", rf=rf),
            "correlation": round(float(np.corrcoef(t.loc[idx], h.loc[idx])[0, 1]), 3),
        }
    return out


def main() -> None:
    close = load_close()
    spy_raw = close[BENCHMARK].pct_change(fill_method=None)
    rf = load_rf_daily()
    weights = target_weights(close)
    first_live = weights.index[(weights.sum(axis=1) > 0).to_numpy().argmax()]
    cells = cell_windows(first_live, close.index[-1])

    report: dict = {
        "study": "haa",
        "pre_registration": REGISTRATION,
        "objective_class": OBJECTIVE_CLASS,
        "benchmark": "raw SPY close-to-close total return (state/history/SPY.parquet), zero cost",
        "instruments": INSTRUMENTS,
        "first_live_day": first_live.date().isoformat(),
        "first_bar": {s: close[s].first_valid_index().date().isoformat() for s in INSTRUMENTS},
        "forms": {},
    }
    for label, cost in (("haa_balanced_8bps", COST_BPS), ("haa_balanced_20bps", COST_BPS_STRESS)):
        returns, diag = simulate_sleeve(weights, close, cost_bps=cost)
        judged = {name: (judge_cell(spy_raw, returns, bounds, rf=rf, bench_label=BENCHMARK, cand_label=label)
                         if bounds is not None else None)
                  for name, bounds in cells.items()}
        report["forms"][label] = {
            "cost_bps": cost,
            "cells": judged,
            "gate_all_cells": gate_all_cells_per_band(judged),
            "stress": _stress_flag(judged),
            "diagnostics": diagnostics(weights, returns, diag, spy_raw, first_live),
        }
        if label == "haa_balanced_8bps":
            haa_returns = returns
    sim_spy = spy_like_for_like(close, first_live)
    report["simulated_spy_diagnostic"] = {
        name: returns_summary(_align(spy_raw, sim_spy, bounds)[1], "SPY_simulated", rf=rf)
        for name, bounds in cells.items() if bounds is not None and not name.startswith("stress:")
    }
    report["tsmom_comparison"] = tsmom_comparison(close.index[-1], rf, spy_raw, haa_returns)

    primary = report["forms"]["haa_balanced_8bps"]
    heldout = primary["cells"]["heldout_2023_plus"]
    report["post_publication_check"] = {
        "published": 2022,
        "window": windows.SCREEN_WINDOWS["heldout_2023_plus"],
        "beat_spy_cagr": bool(heldout["candidate"]["cagr"] > heldout["benchmark"]["cagr"]),
        "candidate": {k: heldout["candidate"][k] for k in ("cagr", "sharpe", "excess_sharpe", "max_dd")},
        "benchmark": {k: heldout["benchmark"][k] for k in ("cagr", "sharpe", "excess_sharpe", "max_dd")},
        "gate": heldout["gate"],
    }
    passed = primary["gate_all_cells"]["passed"] and not bool(primary["stress"].get("fails_stress"))
    report["decision"] = "sleeve_screened_for_mixes" if passed else "sleeve_rejected"
    report["decision_rule"] = (
        "screened iff the balanced HAA form passes benchmark_beater in full + heldout_2023_plus + "
        "early_2020_2022 at 8 bps against raw SPY and its GFC max drawdown is inside the paired band"
    )
    report["limitations"] = [
        "Published 2022 (Keller & Keuning); heldout_2023_plus is the only post-publication evidence — see post_publication_check.",
        "Full-sample start is bounded by the youngest instrument's first bar (see first_bar); GFC coverage begins at that date, not at the window start, if later.",
        "Monthly decision on the last trading day, applied next day; the engine runs daily and would trade the same target the next full run.",
        "Equal-weight top-4 concentration: 25% single-ETF slots; the simulator applies the 0.80 index-ETF elevated cap, never binding at 25%.",
        "Trading-day lookbacks (21/63/126/252) stand in for the paper's calendar months.",
        "Costs 8 bps per unit turnover (registration ETF rate), 20 bps stress reported.",
    ]
    report["reproduce"] = [
        ".venv/bin/python -m backtest.haa_study",
        ".venv/bin/python -m pytest tests/test_haa_study.py -q",
    ]
    REPORT.write_text(json.dumps(report, indent=2, default=_json_default))

    for label, form in report["forms"].items():
        print(f"{label}: passed={form['gate_all_cells']['passed']} fails_stress={form['stress'].get('fails_stress')}")
        for c, v in form["cells"].items():
            if v is None:
                print(f"  {c}: not_evaluable"); continue
            print(f"  {c}: {v['candidate']['cagr']:.1%}/{v['candidate'].get('excess_sharpe', float('nan')):.2f}/{v['candidate']['max_dd']:.1%}"
                  f" vs SPY {v['benchmark']['cagr']:.1%}/{v['benchmark'].get('excess_sharpe', float('nan')):.2f}/{v['benchmark']['max_dd']:.1%}"
                  f" [{v['gate'].get('passed', v['gate'].get('verdict'))}]")
    print("decision:", report["decision"])


if __name__ == "__main__":
    main()
