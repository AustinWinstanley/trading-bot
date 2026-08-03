"""Pre-registered study: is the MOM_LS book a diversified 20 names or one bet?

On 2026-07-23 the live sleeve bought ten longs -- AAOI, BE, DOCN, INTC, LITE,
MU, SNDK, STX, TSEM, WDC -- that were all the same AI-datacenter-hardware
trade. Realized mean pairwise daily-return correlation was 0.56 and basket
volatility was 83% annualized against SPY's 13.5%. Between 07-23 and 07-28
every one of them fell 14-32% while SPY rose 0.4%, and they produced 95% of
the account's realized losses.

``reports/sector_neutral_momentum_feasibility.json`` anticipated exactly this
("reduce unintended sector bets") and deferred, correctly, because a dated
symbol-to-industry map does not exist and present-day sector labels would
inject classification look-ahead.

This study asks the same question using data already in hand. Realized
correlation needs no classification, is computable point-in-time from trailing
returns only, and is a direct measure of the thing that actually hurt: names
that move together. The candidate walks down the momentum ranks and skips any
name whose trailing correlation with an already-selected name exceeds a cap.

The first output is diagnostic and matters regardless of the verdict: was the
live week's 0.56 an outlier, or is the sleeve routinely this concentrated?

Limitations
-----------
* Correlation is estimated on a trailing window and is itself noisy; a cap set
  too low degrades the momentum signal by excluding high-ranked names.
* Skipping correlated names changes which momentum the sleeve holds, so this
  is a signal change, not a pure risk overlay.
* Historical borrowability is unavailable; the universe is survivorship-biased.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.risk_overlay_study import WINDOWS, build_overlay_stream
from backtest.short_capacity_study import profile_returns
from backtest.xsec_data import load

CORR_WINDOW = 60


def realized_concentration(
    weights: pd.DataFrame,
    daily_returns: pd.DataFrame,
    *,
    window: int = CORR_WINDOW,
    sample_every: int = 21,
) -> dict:
    """Mean pairwise correlation inside the long book, sampled through history."""
    longs = weights.gt(0)
    dates = [d for i, d in enumerate(weights.index) if i % sample_every == 0]
    means, vols = [], []
    for date in dates:
        held = list(longs.columns[longs.loc[date]])
        if len(held) < 3:
            continue
        hist = daily_returns.loc[:date, held].tail(window).dropna(axis=1, how="any")
        if hist.shape[1] < 3 or len(hist) < window // 2:
            continue
        corr = hist.corr().values
        iu = np.triu_indices(corr.shape[0], 1)
        means.append(float(np.nanmean(corr[iu])))
        vols.append(float(hist.mean(axis=1).std() * np.sqrt(252)))
    if not means:
        return {}
    arr = np.array(means)
    return {
        "samples": len(arr),
        "mean_pairwise_correlation": float(arr.mean()),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "max": float(arr.max()),
        "pct_of_samples_above_0_5": float((arr >= 0.5).mean()),
        "mean_basket_vol_annualised": float(np.mean(vols)),
    }


VARIANTS = {
    "control (production ranks)": dict(max_correlation=None),
    "corr cap 0.70": dict(max_correlation=0.70),
    "corr cap 0.60": dict(max_correlation=0.60),
    "corr cap 0.50": dict(max_correlation=0.50),
}


def main() -> None:
    close, volume = load()
    close, volume = norm_index(close), norm_index(volume)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in classified["stocks"] if s in close.columns]
    close, volume = close[stocks], volume[stocks]
    daily_returns = close.pct_change()

    streams = build_streams()
    common = 0.40 * streams["spy"] + 0.25 * streams["tsmom"] + 0.20 * streams["trend"]

    results = {}
    for name, kwargs in VARIANTS.items():
        print(f"  simulating {name} ...", flush=True)
        # No stop and no block: this isolates selection. The overlay question is
        # settled separately in reports/risk_overlay_study.json.
        results[name] = build_overlay_stream(
            close, volume, stop_mode="none", block_days=0,
            corr_window=CORR_WINDOW, **kwargs,
        )

    print("  measuring realized concentration ...", flush=True)
    concentration = {
        name: realized_concentration(r.weights, daily_returns)
        for name, r in results.items()
    }

    performance = {}
    for window, slicer in WINDOWS.items():
        performance[window] = []
        for profile in ("base", "2x"):
            for name, result in results.items():
                sliced = profile_returns(common, result, profile=profile).loc[slicer].dropna()
                if sliced.empty:
                    continue
                row = returns_summary(sliced, f"{profile} — {name}")
                row.update(profile=profile, variant=name, **result.diagnostics)
                performance[window].append(row)

    control = "control (production ranks)"
    gates = []
    for name in VARIANTS:
        if name == control:
            continue
        passed = True
        detail = []
        for window in WINDOWS:
            rows = {(r["profile"], r["variant"]): r for r in performance[window]}
            for profile in ("base", "2x"):
                c, v = rows.get((profile, control)), rows.get((profile, name))
                if not c or not v:
                    continue
                ok = v["sharpe"] >= c["sharpe"] and v["cagr"] >= c["cagr"] and v["max_dd"] >= c["max_dd"]
                passed &= ok
                detail.append({
                    "window": window, "profile": profile, "passed": bool(ok),
                    "d_sharpe": v["sharpe"] - c["sharpe"],
                    "d_cagr": v["cagr"] - c["cagr"],
                    "d_max_dd": v["max_dd"] - c["max_dd"],
                })
        gates.append({"variant": name, "passed": bool(passed), "checks": detail})

    winner = next((g["variant"] for g in gates if g["passed"]), None)
    out = {
        "decision": "adopt_" + winner.replace(" ", "_") if winner else "defer",
        "question": (
            "Does capping trailing correlation inside the MOM_LS book improve "
            "risk-adjusted return, and how concentrated is the book by default?"
        ),
        "promotion_rule": (
            "Higher Sharpe, no lower CAGR and no worse max drawdown, in both "
            "account profiles and both the early and held-out windows."
        ),
        "realized_concentration": concentration,
        "gates": gates,
        "performance": performance,
        "limitations": [
            "Trailing correlation is noisy; a low cap degrades the momentum signal.",
            "Skipping correlated names changes the signal, not just its risk.",
            "Historical easy-to-borrow and locate availability is unavailable.",
            "The universe of currently listed companies is survivorship-biased.",
        ],
    }
    path = Path("reports/momentum_concentration_study.json")
    path.write_text(json.dumps(out, indent=2, default=float))
    print(f"wrote {path}")
    print(f"decision: {out['decision']}")


if __name__ == "__main__":
    main()
