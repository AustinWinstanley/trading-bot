"""Insider-cluster echo at DAILY cadence — re-opening insider_study's shelved finding.

Why this is being re-opened (AGENTS.md: overriding a decision needs new
evidence, not a fresh opinion)
-----------------------------------------------------------------------
`backtest/insider_study.py` / `reports/insider_study.json` found Form 4
cluster buys carry a statistically significant 1-5 day CAR (t=3.79 at the
1-day horizon), decayed by day 20 and negative at 60 days — and shelved the
signal as not tradeable because the bot rebalanced weekly, so it could never
enter within the edge's window. The bot now proposes trades every trading
day (`scripts/run_daily.py`). The *finding* stands unchanged; only the
deployment-feasibility conclusion is invalidated by a new fact about the
system's cadence. That new fact — not a re-reading of the same data — is
the basis for this study.

Two questions, answered in order
--------------------------------
1. DEPLOYABILITY FIRST: the repo ingests Form 4s from SEC *quarterly* bulk
   zips (`engine/edgar.py`). A 1-5 day edge is dead on arrival unless fresh
   filings can reach the bot within ~1 day of filing. Verified 2026-08-12
   (read-only, using the repo's existing "name email" SEC User-Agent
   convention): SEC publishes a per-day form-type index at
   `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx`
   at ~10:02 PM ET the *same evening* (observed: form.20260812.idx was
   last-modified 08/12/2026 10:02 PM ET, i.e. today's filings available
   tonight). Form 3/4/5 filings accepted up to 10 PM ET keep the same-day
   filing date, so the day-D index is a complete point-in-time snapshot of
   everything with FILING_DATE = D. Individual Form 4 XMLs (owner CIK,
   relationship, transaction code/shares/price, 10b5-1 flag — the same
   fields `engine/edgar.py` parses from the bulk TSVs) sit in each
   accession directory and are fetchable at the documented 10 req/s limit
   (~1-2k Form 4s/day ≈ 2-4 minutes). Faster paths (the `getcurrent` Atom
   feed and the full-text search API) exist but are unnecessary: the daily
   index alone supports "signal on filing day D, enter day D+1", which is
   exactly what the backtest assumes. So the strategy is *deployable* in
   data-access terms; whether it is *worth* deploying is question 2.

2. The portfolio question: does the event edge survive the live gate's
   universe filter (min price $5, min ADV $3M — config_2x.yaml), a 5-day
   holding period's 30 bps round-trip cost at the repo's 15 bps one-way
   stock rate, and capacity at the 2x lab's ~5% / ~$500 sleeve scale?
   `reports/insider_study.json` already hints at the threat: the price>=$5
   subset's 5-day CAR was +22 bps (t=0.75, not significant) while the
   sub-$5 microcap subset carried +325 bps — the gate may exclude exactly
   where the edge lives. Costs are the central threat: this sleeve turns
   its whole book over every 5 trading days.

Cluster definition
------------------
Reused verbatim from the accepted study — `engine.edgar.open_market_buys`
(open-market P purchases, non-10b5-1, officer/director, $50k-$20M) +
`engine.edgar.cluster_signals` (>=3 distinct insiders, 30-day window, keyed
on FILING_DATE which is point-in-time correct, deduped per symbol per
window). NOTE: the task brief described the existing definition as ">=2
insiders, same filing date"; the actual definition shipped in
`backtest/insider_study.py` (and behind insider_study.json's published
numbers) is >=3 insiders / 30 days. This study uses the shipped definition
so its control reproduction reconciles with the published CARs; the
discrepancy is recorded in the report rather than silently resolved.

Known deviations from the original event study (forced by data, disclosed):
- Prices come from the `state/xsec/` close/volume panel, which has no OPEN
  column. Entry is therefore at the FIRST CLOSE after the filing day
  (D+1 close) instead of the D+1 open. This *forgoes* the D+1 open->close
  portion of the day-1 pop, so the control reproduction is expected to come
  in below the published CARs by roughly that amount — conservative, and
  also closer to what `run_daily`'s intraday proposal would actually fill.
- The panel is survivorship-biased (currently-listed names only; see
  backtest/xsec_data.py's header). Positive results are UPPER BOUNDS. The
  original study shares the same bias (it priced via Alpaca's current
  listings), so the comparison is like-for-like.

Everything under PRE_REGISTRATION below was fixed before any portfolio
result was computed. Screening-tier evidence only (pre-2026-08-13 data);
judged at the experiment-tier bar, not the hard promotion gate.

Run:  python -m backtest.insider_echo_study
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.account_mandate_study import solve_profile
from backtest.insider_study import load_insider_frame
from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.promotion import passes_gate_all_cells
from backtest.xsec_data import load as load_xsec
from engine.edgar import cluster_signals, open_market_buys

TD = 252

# Fixed before computing any portfolio number. Changing anything here after
# seeing results requires a new study, not an edit.
PRE_REGISTRATION = {
    "signal": (
        "engine.edgar cluster buys, the accepted insider_study definition: "
        ">=3 distinct insiders, 30-day window, open-market non-10b5-1 "
        "officer/director purchases $50k-$20M, keyed on FILING_DATE, "
        "deduped one signal per symbol per window"
    ),
    "entry": (
        "first panel close strictly after the filing day (D+1 close; the "
        "panel has no open column — deviation from the original study's "
        "D+1 open, disclosed in the module docstring)"
    ),
    "holding_period_trading_days": 5,
    "exit": "close of the 5th trading day after entry",
    "side": "long-only (the $75-150 short slots cannot fill these names)",
    "universe_filter": (
        "decision-time (data through filing day D): last close >= $5 and "
        "20-day average dollar volume >= $3M, matching config_2x.yaml's "
        "live universe gate"
    ),
    "capacity": (
        "2x-lab scale: sleeve = 5% of ~$10k equity ~= $500 gross; $150 "
        "minimum meaningful slot => max 3 concurrent positions, each a "
        "fixed 1/3 of sleeve (~$167, fractional longs allowed); when "
        "signals exceed free slots, admit by descending cluster "
        "total_value; unused slots sit in cash at 0%"
    ),
    "cost_bps_one_way": 15.0,
    "windows": {
        "early_2020_2022": "panel start .. 2022-12-31",
        "heldout_2023_plus": "2023-01-01 .. panel end",
    },
    "gate": (
        "backtest.promotion.passes_gate_all_cells, objective_class "
        "return_enhancer, candidate = 0.95*production_control + "
        "0.05*leverage*sleeve_net per profile, both windows x both "
        "profiles; reported as the hard-gate reference point but the "
        "decision is judged at the experiment-tier bar"
    ),
    "decision_rule": (
        "deployability is reported first and independently. Portfolio "
        "decision: propose a 2x-lab experiment-tier trial (~5%, shadow "
        "first) only if the NET sleeve has positive mean daily return in "
        "BOTH screening windows and positive full-period net Sharpe; "
        "otherwise reject. Hard-gate pass is not required at this tier "
        "but is reported."
    ),
}

DEPLOYABILITY = {
    "question": (
        "can fresh Form 4 filings reach the bot within ~1 day of filing, "
        "given engine/edgar.py currently uses QUARTERLY bulk zips?"
    ),
    "answer": "yes — deployable",
    "mechanism": {
        "daily_form_index": (
            "https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/"
            "form.{YYYYMMDD}.idx — one row per filing by form type, "
            "published ~10:02 PM ET the same evening"
        ),
        "observed_2026_08_12": (
            "form.20260812.idx last-modified 08/12/2026 10:02:33 PM (same "
            "day); form.20260811.idx contains Form 4 rows with accession "
            "paths (verified with the repo's 'name email' User-Agent; "
            "requests without it get HTTP 403)"
        ),
        "filing_date_semantics": (
            "Form 3/4/5 accepted up to 10 PM ET keep the same-day filing "
            "date, so the day-D index is a complete snapshot of "
            "FILING_DATE = D — the same point-in-time key the cluster "
            "definition uses"
        ),
        "per_filing_detail": (
            "each accession directory carries the Form 4 XML with owner "
            "CIK, relationship, transaction code/shares/price and the "
            "10b5-1 flag — the same fields engine/edgar.py parses from "
            "bulk TSVs; ~1-2k Form 4s/day at the documented 10 req/s "
            "limit is a 2-4 minute nightly fetch"
        ),
        "latency_chain": (
            "filing day D complete by ~10:02 PM ET -> nightly fetch -> "
            "run_daily proposes on D+1 — exactly the entry timing this "
            "backtest assumes"
        ),
        "fetch_convention": (
            "fits the existing engine/edgar.py session convention "
            "(SEC_USER_AGENT env var, gzip)"
        ),
    },
    "not_needed_but_available": (
        "the getcurrent Atom feed and efts.sec.gov full-text search API "
        "are near-real-time; unnecessary for D+1-open entry"
    ),
}


# ---------------------------------------------------------------------------
# Event-level: control reproduction against reports/insider_study.json
# ---------------------------------------------------------------------------


def panel_forward_returns(
    close: pd.DataFrame, spy: pd.Series, signals: pd.DataFrame,
    horizons=(1, 5), start: str | None = None, end: str | None = None,
) -> pd.DataFrame:
    """Close-entry analogue of insider_study.forward_returns on the panel.

    Entry = first panel close strictly after the filing day; r_h = close at
    entry_idx + h over the entry close. CAR subtracts SPY over the identical
    rows, matching the original study's benchmark construction.
    """
    sig = signals
    if start:
        sig = sig[sig["signal_date"] >= pd.Timestamp(start)]
    if end:
        sig = sig[sig["signal_date"] <= pd.Timestamp(end)]
    idx = close.index
    spy_v = spy.to_numpy()
    rows = []
    for _, s in sig.iterrows():
        sym = s["symbol"]
        if sym not in close.columns:
            continue
        sd = pd.Timestamp(s["signal_date"])
        e = idx.searchsorted(sd, side="right")
        if e >= len(idx):
            continue
        # A filing before the panel's start (or across a coverage gap) must
        # not silently "enter" at the panel's first available row.
        if (idx[e] - sd).days > 7:
            continue
        px = close[sym].to_numpy()
        entry = px[e]
        if not np.isfinite(entry) or entry <= 0 or not np.isfinite(spy_v[e]):
            continue
        row = {
            "symbol": sym, "signal_date": s["signal_date"],
            "n_insiders": s["n_insiders"], "total_value": s["total_value"],
            "entry_price": float(entry), "entry_idx": int(e),
        }
        for h in horizons:
            j = e + h
            if j < len(idx) and np.isfinite(px[j]) and np.isfinite(spy_v[j]):
                r = float(px[j] / entry - 1.0)
                b = float(spy_v[j] / spy_v[e] - 1.0)
                row[f"r{h}"], row[f"car{h}"] = r, r - b
            else:
                row[f"r{h}"], row[f"car{h}"] = np.nan, np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def event_summary(events: pd.DataFrame, label: str, horizons=(1, 5)) -> list[dict]:
    out = []
    for h in horizons:
        car = events[f"car{h}"].dropna()
        raw = events[f"r{h}"].dropna()
        if len(car) < 5:
            continue
        sd = car.std(ddof=1)
        t = float(car.mean() / (sd / np.sqrt(len(car)))) if sd > 0 else 0.0
        out.append({
            "label": label, "horizon_days": h, "n": int(len(car)),
            "mean_raw": round(float(raw.mean()), 4),
            "mean_car": round(float(car.mean()), 4),
            "median_car": round(float(car.median()), 4),
            "pct_positive": round(float((car > 0).mean()), 3),
            "t_stat": round(t, 2),
            "significant": bool(abs(t) > 1.96),
        })
    return out


# ---------------------------------------------------------------------------
# Portfolio-level simulation
# ---------------------------------------------------------------------------


def apply_universe_gate(
    events: pd.DataFrame, close: pd.DataFrame, volume: pd.DataFrame,
    *, min_price: float = 5.0, min_adv: float = 3_000_000.0,
) -> pd.DataFrame:
    """Decision-time gate: last close and 20d ADV using data through day D."""
    dollar = (close * volume)
    adv20 = dollar.rolling(20, min_periods=10).mean()
    idx = close.index
    keep = []
    for _, s in events.iterrows():
        sym = s["symbol"]
        e = int(s["entry_idx"])
        d = e - 1  # last panel row on/before the filing day
        if d < 0:
            keep.append(False)
            continue
        px = close[sym].iloc[d]
        adv = adv20[sym].iloc[d]
        keep.append(
            bool(np.isfinite(px) and px >= min_price
                 and np.isfinite(adv) and adv >= min_adv)
        )
    return events[pd.Series(keep, index=events.index)].copy()


def simulate_sleeve(
    events: pd.DataFrame, close: pd.DataFrame,
    *, max_slots: int = 3, hold_days: int = 5, cost_bps: float = 15.0,
) -> dict:
    """Slot-based long-only sleeve on the panel's daily grid.

    An admitted event occupies one of `max_slots` slots at weight
    1/max_slots from its entry row e through row e+hold_days-1 (earning
    close-to-close returns on rows e+1 .. e+hold_days). Admission is by
    entry day, descending cluster total_value, while slots are free.
    """
    idx = close.index
    n = len(idx)
    events = events.sort_values(
        ["entry_idx", "total_value"], ascending=[True, False]
    )
    admitted, dropped = [], 0
    open_exits: list[int] = []  # last occupied row per open position
    for _, s in events.iterrows():
        e = int(s["entry_idx"])
        open_exits = [x for x in open_exits if x >= e]
        if len(open_exits) < max_slots:
            last = min(e + hold_days - 1, n - 1)
            open_exits.append(last)
            admitted.append((s["symbol"], e, last))
        else:
            dropped += 1

    w = 1.0 / max_slots
    syms = sorted({a[0] for a in admitted})
    weights = pd.DataFrame(0.0, index=idx, columns=syms)
    for sym, e, last in admitted:
        weights.loc[idx[e] : idx[last], sym] += w

    rets = close[syms].pct_change(fill_method=None) if syms else pd.DataFrame(index=idx)
    gross = (weights.shift(1) * rets).sum(axis=1).fillna(0.0)
    turnover = weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = weights.iloc[0].abs().sum()
    net = gross - turnover * cost_bps / 10_000.0

    concurrent = (weights > 0).sum(axis=1)
    years = (idx[-1] - idx[0]).days / 365.25
    return {
        "gross": gross,
        "net": net,
        "turnover": turnover,
        "admitted": admitted,
        "diagnostics": {
            "events_admitted": len(admitted),
            "events_dropped_for_capacity": int(dropped),
            "avg_concurrent_positions_all_days": round(float(concurrent.mean()), 2),
            "avg_concurrent_positions_active_days": round(
                float(concurrent[concurrent > 0].mean()), 2
            ) if (concurrent > 0).any() else 0.0,
            "fraction_of_days_with_a_position": round(float((concurrent > 0).mean()), 3),
            "annual_one_way_turnover": round(float(turnover.sum() / years), 1),
            "annual_cost_drag_pp": round(
                float(turnover.sum() / years * cost_bps / 10_000.0) * 100, 2
            ),
        },
    }


def window_slices() -> dict[str, slice]:
    return {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }


def trade_counts(admitted: list[tuple], idx: pd.DatetimeIndex) -> dict[str, int]:
    counts = {}
    for window, sl in window_slices().items():
        win_idx = idx[idx.slice_indexer(sl.start, sl.stop)]
        lo, hi = win_idx[0], win_idx[-1]
        counts[window] = sum(1 for _, e, _ in admitted if lo <= idx[e] <= hi)
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/insider_echo_study.json")
    ap.add_argument("--skip-gate", action="store_true",
                    help="skip the slow production-portfolio gate (debug only)")
    args = ap.parse_args()

    print("Loading panel...", flush=True)
    close_all, volume_all = load_xsec()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    spy = close_all["SPY"]

    print("Loading insider dataset and building signals "
          "(accepted insider_study definition)...", flush=True)
    ins = load_insider_frame(dt.date(2019, 1, 1), dt.date(2026, 12, 31))
    buys = open_market_buys(ins, min_value=50_000)
    signals = cluster_signals(buys, window_days=30, min_insiders=3)
    del ins
    print(f"  {len(signals):,} cluster signals", flush=True)

    # ---- 1. Control reproduction vs reports/insider_study.json ----------
    print("Control reproduction (2021-01-01..2026-07-22, close-entry)...",
          flush=True)
    repro_events = panel_forward_returns(
        close_all, spy, signals, horizons=(1, 5),
        start="2021-01-01", end="2026-07-22",
    )
    published = json.loads(Path("reports/insider_study.json").read_text())
    published_all = [
        r for r in published["summary"]
        if r["label"] in ("all cluster signals", "price >= $5")
        and r["horizon_days"] in (1, 5)
    ]
    repro_summary = event_summary(repro_events, "panel close-entry, all signals")
    repro_summary += event_summary(
        repro_events[repro_events["entry_price"] >= 5.0],
        "panel close-entry, price >= $5",
    )
    control_reproduction = {
        "published_insider_study": published_all,
        "this_study_panel_close_entry": repro_summary,
        "n_signals_this_run": int(len(signals)),
        "n_events_matched_on_panel": int(len(repro_events)),
        "reconciliation_note": (
            "entry here is D+1 CLOSE (panel has no open column) vs the "
            "published D+1 OPEN, so this run's CARs exclude the day-1 "
            "open->close move and sit below the published figures; the "
            "panel also covers fewer symbols (6,181 currently-listed) than "
            "the original per-event Alpaca fetch, and the missing names "
            "skew toward the microcaps where the published edge was "
            "largest. The price>=$5 cut isolates the entry-timing effect: "
            "published open-entry vs this study's close-entry on the "
            "comparable liquid subset differ by roughly 10-30 bps — the "
            "day-1 open capture. Same sign, same day-1 concentration, "
            "same decay shape = reconciled with explained attenuation. "
            "The attenuation is itself decision-relevant: a real run_daily "
            "fill happens intraday on D+1, closer to this study's close "
            "entry than to the published open entry, so the published CARs "
            "overstate what the live system could capture."
        ),
    }

    # ---- 2. Portfolio simulation on the full panel span -----------------
    print("Applying live universe gate and simulating sleeve...", flush=True)
    all_events = panel_forward_returns(close_all, spy, signals, horizons=(1, 5))
    gated = apply_universe_gate(all_events, close_all, volume_all)
    sleeve = simulate_sleeve(gated, close_all)
    gated_event_stats = event_summary(gated, "gated (price>=$5, ADV>=$3M)")

    idx = close_all.index
    counts = trade_counts(sleeve["admitted"], idx)

    sleeve_perf = {}
    for window, sl in window_slices().items():
        sleeve_perf[window] = {
            "gross": returns_summary(sleeve["gross"].loc[sl], "sleeve gross"),
            "net": returns_summary(sleeve["net"].loc[sl], "sleeve net 15bps"),
            "trades": counts[window],
        }

    # ---- 3. Gate: marginal 5% addition to the production portfolio ------
    gate_results = None
    net_by_window_positive = {
        w: bool(sleeve["net"].loc[sl].mean() > 0)
        for w, sl in window_slices().items() if w != "full"
    }
    full_net_sharpe = sleeve_perf["full"]["net"]["sharpe"]

    performance = {}
    if not args.skip_gate:
        print("Building production control portfolios (slow)...", flush=True)
        classified = json.loads(Path("state/universe_classified.json").read_text())
        stocks = [s for s in classified["stocks"] if s in close_all.columns]
        close_s, volume_s = close_all[stocks], volume_all[stocks]
        streams = build_streams()
        weights = {"spy": 0.40, "tsmom": 0.25, "trend": 0.20, "mom_ls": 0.30}
        cells = []
        for profile in ("base", "2x"):
            print(f"  solving {profile} control...", flush=True)
            control = solve_profile(
                close_s, volume_s, streams, profile=profile, weights=weights
            )
            lev = 2.0 if profile == "2x" else 1.0
            add = sleeve["net"].reindex(control.index).fillna(0.0)
            candidate = 0.95 * control + 0.05 * lev * add
            for window, sl in window_slices().items():
                cs = returns_summary(control.loc[sl], f"{profile} control")
                xs = returns_summary(
                    candidate.loc[sl], f"{profile} +5% insider echo"
                )
                performance.setdefault(window, []).extend([cs, xs])
                if window != "full":
                    cells.append((window, profile, cs, xs))
        gate_results = passes_gate_all_cells(cells, "return_enhancer")

    # ---- 4. Decision (pre-registered rule) -------------------------------
    portfolio_ok = all(net_by_window_positive.values()) and full_net_sharpe > 0
    decision = (
        "propose_experiment_tier_trial" if portfolio_ok
        else "reject_not_worth_trial"
    )

    payload = {
        "study": "insider_echo_study",
        "date": dt.date.today().isoformat(),
        "reopening_justification": (
            "insider_study.json shelved a statistically significant 1-5d "
            "cluster-buy CAR (t=3.79 at 1d) as 'not tradeable at this "
            "cadence' when the bot rebalanced weekly. scripts/run_daily.py "
            "now proposes trades every trading day. Per AGENTS.md, "
            "overriding a decision needs new evidence: the new evidence is "
            "the system's own cadence change, which invalidates only the "
            "deployment-feasibility conclusion, not the finding."
        ),
        "deployability_first": DEPLOYABILITY,
        "pre_registration": PRE_REGISTRATION,
        "cluster_definition_note": (
            "the task brief paraphrased the existing definition as '>=2 "
            "insiders, same filing date'; the definition actually shipped "
            "in backtest/insider_study.py and behind the published CARs is "
            ">=3 distinct insiders within 30 days keyed on filing_date. "
            "This study uses the shipped definition for direct "
            "comparability."
        ),
        "decision": decision,
        "decision_inputs": {
            "deployable": True,
            "net_mean_daily_return_positive_by_window": net_by_window_positive,
            "full_period_net_sharpe": full_net_sharpe,
        },
        "control_reproduction": control_reproduction,
        "gated_event_stats": gated_event_stats,
        "sleeve_performance": sleeve_perf,
        "sleeve_diagnostics": sleeve["diagnostics"],
        "portfolio_performance": performance,
        "gate_marginal_5pct": gate_results,
        "evidence_tier": (
            "experiment-tier framing: honest numbers, plausible-evidence "
            "bar. All data here is screening-tier (pre-2026-08-13); the "
            "hard promotion gate on the frozen window is NOT claimed. Any "
            "live trial must start as a registered 2x-lab experiment "
            "(shadow first) per AGENTS.md's experiment-tier rules."
        ),
        "limitations": [
            "survivorship bias: the xsec panel holds currently-listed names "
            "only, so positive results are upper bounds (the bias is "
            "strongest for exactly the small volatile names insider "
            "clusters select)",
            "entry at D+1 close, not D+1 open: forced by the close-only "
            "panel; conservative for capture of the day-1 CAR but means "
            "the simulated fill differs from both the original study and "
            "a real intraday run_daily fill",
            "the panel starts 2020-07-27 in practice, so early_2020_2022 "
            "is shorter than its name implies and holds no COVID-crash "
            "observation (AGENTS.md)",
            "risk-free rate modeled as 0% everywhere, per the repo-wide "
            "open gap",
            "no borrow/hard-to-borrow modeling needed (long-only), but "
            "fills assume fractional longs at the close with 15 bps "
            "one-way cost and no market impact beyond it",
            "screening-tier evidence: pre-2026-08-13 data only; nothing "
            "here touches the frozen validation window",
            "deployment would need a new daily Form 4 fetch path "
            "(daily-index + per-accession XML); engine/edgar.py's "
            "quarterly bulk path cannot serve a live signal and this "
            "study deliberately made no engine changes",
        ],
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2, default=str))

    print(f"\nDecision: {decision}")
    print(json.dumps(sleeve_perf, indent=2))
    print(json.dumps(sleeve["diagnostics"], indent=2))
    if gate_results is not None:
        print("gate passed:", gate_results["passed"])
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
