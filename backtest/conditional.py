"""Regime-conditional signal weighting — does confidence-scaling actually help?

The idea is sound: a signal's edge is not constant, so scale exposure up when
conditions favour it and down when they don't. The danger is that this is also
the easiest possible place to overfit. Every conditioning rule adds parameters,
and with enough of them any sample can be made to look excellent in hindsight.

Guardrails used here, without which none of this is worth reading:

1. **Every rule comes from a prior, not from the data.** Each conditioner below
   is specified from published research before being run, not selected because
   it scored well. Fitting the rule to the sample and then reporting the
   sample's result is circular.
2. **Judged out-of-sample.** Each rule is evaluated on the second half of the
   sample only. In-sample improvement is reported alongside, purely to show the
   gap — an in-sample gain that vanishes out-of-sample is the signature of
   overfitting, and seeing both numbers is how you catch it.
3. **Compared against the static baseline.** A conditioner has to beat simply
   holding the signal at constant weight. Most do not.

Conditioners:
  vol_target     Moreira & Muir (2017), volatility-managed portfolios: scale
                 inversely to the signal's own recent realised volatility.
  mom_crash      Daniel & Moskowitz (2016), "Momentum Crashes": momentum
                 collapses during sharp rebounds *after* bear markets. Cut
                 exposure when the market is below its 200DMA and volatile.
  dispersion     More cross-sectional spread means more to exploit; scale with
                 the dispersion of the signal across the universe.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.signal_library import (
    SIGNALS,
    eligibility,
    neutral_portfolio,
    sharpe,
)
from backtest.xsec_data import load

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# Conditioners: return a per-day exposure multiplier in roughly [0, 2]
# --------------------------------------------------------------------------


def cond_vol_target(sig_ret: pd.Series, ctx: dict, *, target: float = 0.10,
                    window: int = 63, cap: float = 2.0) -> pd.Series:
    """Scale to a constant volatility target using the signal's own trailing vol."""
    realised = sig_ret.rolling(window, min_periods=30).std() * np.sqrt(TRADING_DAYS)
    mult = (target / realised).clip(upper=cap)
    return mult.shift(1).fillna(1.0)          # yesterday's vol, traded today


def cond_momentum_crash(sig_ret: pd.Series, ctx: dict, *, cut: float = 0.5) -> pd.Series:
    """Daniel-Moskowitz: cut exposure in a stressed, below-trend market."""
    mkt, mkt_vol = ctx["mkt"], ctx["mkt_vol"]
    bear = mkt < mkt.rolling(200, min_periods=150).mean()
    stressed = mkt_vol > mkt_vol.rolling(252, min_periods=150).median()
    danger = (bear & stressed).reindex(sig_ret.index).fillna(False)
    return pd.Series(np.where(danger, cut, 1.0), index=sig_ret.index).shift(1).fillna(1.0)


def cond_dispersion(sig_ret: pd.Series, ctx: dict, *, cap: float = 2.0) -> pd.Series:
    """Scale with cross-sectional dispersion of the signal."""
    disp = ctx["dispersion"].reindex(sig_ret.index).ffill()
    rel = disp / disp.rolling(252, min_periods=100).median()
    return rel.clip(upper=cap).shift(1).fillna(1.0)


CONDITIONERS = {
    "vol_target": cond_vol_target,
    "mom_crash": cond_momentum_crash,
    "dispersion": cond_dispersion,
}


def evaluate(sig_ret: pd.Series, mult: pd.Series, name: str, split: int) -> dict:
    """Static vs conditioned, reported in-sample and out-of-sample separately."""
    conditioned = sig_ret * mult
    # Rescale to the static stream's volatility so the comparison is about
    # timing, not about having quietly taken more risk.
    v_s, v_c = sig_ret.std(), conditioned.std()
    if v_c > 0:
        conditioned = conditioned * (v_s / v_c)

    return {
        "conditioner": name,
        "static_IS": round(sharpe(sig_ret.iloc[:split]), 3),
        "cond_IS": round(sharpe(conditioned.iloc[:split]), 3),
        "static_OOS": round(sharpe(sig_ret.iloc[split:]), 3),
        "cond_OOS": round(sharpe(conditioned.iloc[split:]), 3),
        "IS_gain": round(sharpe(conditioned.iloc[:split]) - sharpe(sig_ret.iloc[:split]), 3),
        "OOS_gain": round(sharpe(conditioned.iloc[split:]) - sharpe(sig_ret.iloc[split:]), 3),
    }


def main() -> None:
    close, volume = load()
    cls = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in cls["stocks"] if s in close.columns]
    px, vol = close[stocks], volume[stocks]
    elig = eligibility(px, vol)

    # Market context, all strictly backward-looking.
    mkt = close["SPY"] if "SPY" in close.columns else px.mean(axis=1)
    ctx = {
        "mkt": mkt,
        "mkt_vol": mkt.pct_change().rolling(21, min_periods=10).std() * np.sqrt(TRADING_DAYS),
    }

    # Only signals that survived the gross/net + sign-stability screen are worth
    # conditioning. Conditioning a signal with no edge just reallocates noise.
    survivors = ["mom_12_1", "mom_6_1"]

    all_rows = []
    for sig_name in survivors:
        score = SIGNALS[sig_name](px, vol)
        r = neutral_portfolio(score, px, elig, rebalance=21, cost_bps=15).iloc[260:]
        ctx["dispersion"] = score.where(elig).std(axis=1)
        split = len(r) // 2

        print(f"\n{'=' * 78}\n{sig_name}: static Sharpe {sharpe(r):+.3f} "
              f"(IS {sharpe(r.iloc[:split]):+.3f} / OOS {sharpe(r.iloc[split:]):+.3f})\n{'=' * 78}")
        rows = []
        for cname, fn in CONDITIONERS.items():
            mult = fn(r, ctx)
            row = evaluate(r, mult, cname, split)
            row["signal"] = sig_name
            rows.append(row)
        # All three stacked
        stacked = pd.Series(1.0, index=r.index)
        for fn in CONDITIONERS.values():
            stacked = stacked * fn(r, ctx)
        row = evaluate(r, stacked, "ALL stacked", split)
        row["signal"] = sig_name
        rows.append(row)

        df = pd.DataFrame(rows)[
            ["conditioner", "static_IS", "cond_IS", "IS_gain", "static_OOS", "cond_OOS", "OOS_gain"]
        ]
        pd.set_option("display.width", 170)
        print(df.to_string(index=False))
        all_rows.extend(rows)

    out = pd.DataFrame(all_rows)
    print(f"\n{'=' * 78}\nVERDICT\n{'=' * 78}")
    helps = out[out.OOS_gain > 0]
    print(f"  conditioners improving OOS Sharpe: {len(helps)}/{len(out)}")
    if len(out):
        print(f"  mean IS gain  {out.IS_gain.mean():+.3f}")
        print(f"  mean OOS gain {out.OOS_gain.mean():+.3f}")
        print("\n  A large IS gain with a small or negative OOS gain is the")
        print("  signature of fitting the sample rather than finding an effect.")

    Path("reports").mkdir(exist_ok=True)
    Path("reports/conditional.json").write_text(json.dumps(all_rows, indent=2, default=str))
    print("\nWrote reports/conditional.json")


if __name__ == "__main__":
    main()
