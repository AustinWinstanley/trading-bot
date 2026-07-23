"""A library of cross-sectional signals, evaluated by correlation.

The premise, corrected from earlier work in this repo: a single strong signal
is not the goal. Uncorrelated Sharpes add in quadrature, so N genuinely
independent signals of Sharpe s combine toward s*sqrt(N). Ten mediocre,
uncorrelated signals beat one good one — and that, not superior information, is
the actual technology behind systematic funds.

Every signal here is built **market-neutral**: rank the universe, go long the
top decile and short the bottom, equal weight. That construction strips out
market beta, which is what makes the correlation between signals meaningful. A
long-only signal is mostly measuring the market and will correlate ~0.6+ with
every other long-only signal regardless of its content.

All signals are computed from the close/volume matrices, are strictly
point-in-time (every input is lagged before it is used to form a position), and
charge turnover costs.

    python -m backtest.signal_library
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.xsec_data import load

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# Signal definitions — each returns a (dates x symbols) score, higher = long
# --------------------------------------------------------------------------


def sig_momentum_12_1(close, volume):
    """Classic Jegadeesh-Titman: 12-month return skipping the last month."""
    return (close.shift(21) / close.shift(252)) - 1


def sig_momentum_6_1(close, volume):
    return (close.shift(21) / close.shift(126)) - 1


def sig_reversal_1m(close, volume):
    """Short-term reversal — last month's losers outperform. Sign is negative
    momentum; our own drift study found buying strength loses, so this is that
    finding expressed as a tradeable cross-sectional signal."""
    return -((close / close.shift(21)) - 1)


def sig_reversal_1w(close, volume):
    return -((close / close.shift(5)) - 1)


def sig_low_volatility(close, volume):
    """Low-volatility anomaly: low-vol names earn more per unit of risk."""
    return -close.pct_change().rolling(126, min_periods=60).std()


def sig_max_lottery(close, volume):
    """MAX effect (Bali-Cakici-Whitelaw): stocks with extreme recent single-day
    gains are over-bought by lottery-seekers and subsequently underperform."""
    return -close.pct_change().rolling(21, min_periods=15).max()


def sig_idio_vol(close, volume):
    """Idiosyncratic vol vs the equal-weight market — high idio vol underperforms."""
    r = close.pct_change()
    mkt = r.mean(axis=1)
    resid = r.sub(mkt, axis=0)
    return -resid.rolling(63, min_periods=40).std()


def sig_52w_high(close, volume):
    """Proximity to the 52-week high — a momentum variant that anchors on level."""
    return close.shift(1) / close.shift(1).rolling(252, min_periods=200).max()


def sig_volume_shock(close, volume):
    """Abnormal volume without price confirmation tends to mean-revert."""
    return -(volume.shift(1) / volume.shift(1).rolling(60, min_periods=30).mean())


def sig_illiquidity(close, volume):
    """Amihud illiquidity: |return| per dollar traded. Illiquid names carry a
    premium — and this is the one signal that is structurally *unavailable* to
    large funds, which is exactly why it might survive."""
    r = close.pct_change().abs()
    dv = (close * volume).replace(0, np.nan)
    return (r / dv).rolling(63, min_periods=40).mean()


def sig_acceleration(close, volume):
    """Is momentum itself improving? Recent 1m return minus prior 1m return."""
    m1 = (close.shift(1) / close.shift(21)) - 1
    m2 = (close.shift(21) / close.shift(42)) - 1
    return m1 - m2


def sig_downside_vol(close, volume):
    """Semi-deviation — penalise names whose losses cluster."""
    r = close.pct_change()
    return -r.where(r < 0).rolling(126, min_periods=50).std()


SIGNALS = {
    "mom_12_1": sig_momentum_12_1,
    "mom_6_1": sig_momentum_6_1,
    "reversal_1m": sig_reversal_1m,
    "reversal_1w": sig_reversal_1w,
    "low_vol": sig_low_volatility,
    "max_lottery": sig_max_lottery,
    "idio_vol": sig_idio_vol,
    "high_52w": sig_52w_high,
    "volume_shock": sig_volume_shock,
    "illiquidity": sig_illiquidity,
    "acceleration": sig_acceleration,
    "downside_vol": sig_downside_vol,
}


# --------------------------------------------------------------------------
# Portfolio construction
# --------------------------------------------------------------------------


def eligibility(close, volume, *, min_price=5.0, min_dollar_volume=5e6, lag=1):
    dv = (close * volume).rolling(20, min_periods=10).mean()
    return (
        close.shift(lag).gt(min_price)
        & dv.shift(lag).gt(min_dollar_volume)
        & close.notna()
    )


def neutral_portfolio(
    score: pd.DataFrame,
    close: pd.DataFrame,
    elig: pd.DataFrame,
    *,
    decile: float = 0.1,
    rebalance: int = 5,
    cost_bps: float = 15.0,
) -> pd.Series:
    """Long top decile / short bottom decile, equal weight, dollar-neutral."""
    s = score.where(elig)
    # Only rebalance every `rebalance` days; hold in between.
    rebal_idx = np.zeros(len(s), dtype=bool)
    rebal_idx[::rebalance] = True

    ranks = s.rank(axis=1, pct=True)
    longs = ranks.ge(1 - decile)
    shorts = ranks.le(decile)

    n_long = longs.sum(axis=1).replace(0, np.nan)
    n_short = shorts.sum(axis=1).replace(0, np.nan)
    w = longs.div(n_long, axis=0).fillna(0) - shorts.div(n_short, axis=0).fillna(0)
    w = w.where(pd.Series(rebal_idx, index=s.index), np.nan).ffill().fillna(0)

    # Lag the weights by one day: a score computed from data through today can
    # only be traded tomorrow.
    ret = (w.shift(1) * close.pct_change()).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1)
    return (ret - turnover * cost_bps / 10_000.0).fillna(0)


def sharpe(r: pd.Series) -> float:
    v = r.std() * np.sqrt(TRADING_DAYS)
    return float((r.mean() * TRADING_DAYS) / v) if v > 0 else 0.0


def summarize(r: pd.Series, name: str) -> dict:
    eq = (1 + r).cumprod()
    mx = eq.cummax()
    return {
        "signal": name,
        "sharpe": round(sharpe(r), 3),
        "ann_ret": round(float(r.mean() * TRADING_DAYS), 4),
        "vol": round(float(r.std() * np.sqrt(TRADING_DAYS)), 4),
        "max_dd": round(float(((eq - mx) / mx).min()), 4),
        "hit_rate": round(float((r > 0).mean()), 3),
    }


def diagnose(score, close, elig, *, cost_bps=15.0):
    """The two tests that separate a real signal from an artefact.

    GROSS vs NET: run the same signal at zero cost and at realistic cost. If the
    Sharpe only appears once costs are applied, it is turnover drag, not
    predictive content. `volume_shock` scored -2.19 net and -0.02 gross — the
    entire "signal" was the cost of churning, and inverting it would have lost
    money just as fast.

    SIGN STABILITY: split the sample in half. A signal whose sign flips between
    halves is fitting a regime, not an effect. The whole low-volatility cluster
    flips here (+0.36 then -0.55) because 2020-21 was a speculative melt-up.
    """
    out = {}
    for label, reb in (("wk", 5), ("mo", 21)):
        gross = neutral_portfolio(score, close, elig, rebalance=reb, cost_bps=0).iloc[260:]
        net = neutral_portfolio(score, close, elig, rebalance=reb, cost_bps=cost_bps).iloc[260:]
        out[f"gross_{label}"] = round(sharpe(gross), 3)
        out[f"net_{label}"] = round(sharpe(net), 3)
        if reb == 21:
            h = len(gross) // 2
            out["oos_1st"] = round(sharpe(gross.iloc[:h]), 3)
            out["oos_2nd"] = round(sharpe(gross.iloc[h:]), 3)
            out["sign_stable"] = bool(np.sign(out["oos_1st"]) == np.sign(out["oos_2nd"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebalance", type=int, default=5)
    ap.add_argument("--decile", type=float, default=0.1)
    ap.add_argument("--cost-bps", type=float, default=15.0)
    ap.add_argument("--out", default="reports/signal_library.json")
    args = ap.parse_args()

    close, volume = load()
    cls = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in cls["stocks"] if s in close.columns]
    close, volume = close[stocks], volume[stocks]
    print(f"universe: {len(stocks):,} operating companies, "
          f"{close.index[0].date()} -> {close.index[-1].date()}\n")

    elig = eligibility(close, volume)
    print(f"median eligible names/day: {int(elig.sum(axis=1).median()):,}\n")

    streams, rows = {}, []
    for name, fn in SIGNALS.items():
        score = fn(close, volume)
        r = neutral_portfolio(score, close, elig, decile=args.decile,
                              rebalance=args.rebalance, cost_bps=args.cost_bps)
        r = r.iloc[260:]  # discard warm-up
        streams[name] = r
        rows.append(summarize(r, name))
        print(f"  {name:14} sharpe {rows[-1]['sharpe']:+.3f}  ann {rows[-1]['ann_ret']:+7.2%}  "
              f"dd {rows[-1]['max_dd']:+.1%}")

    df = pd.DataFrame(streams).dropna()
    pd.set_option("display.width", 220)

    print("\n" + "=" * 92)
    print("STANDALONE (market-neutral long/short deciles)")
    print("=" * 92)
    print(pd.DataFrame(rows).sort_values("sharpe", ascending=False).to_string(index=False))

    print("\n" + "=" * 92)
    print("DIAGNOSTIC: gross vs net, and sign stability across halves")
    print("=" * 92)
    diag = []
    for name, fn in SIGNALS.items():
        d = diagnose(fn(close, volume), close, elig, cost_bps=args.cost_bps)
        d["signal"] = name
        diag.append(d)
    diag_df = pd.DataFrame(diag)[
        ["signal", "gross_wk", "net_wk", "gross_mo", "net_mo", "oos_1st", "oos_2nd", "sign_stable"]
    ].sort_values("net_mo", ascending=False)
    print(diag_df.to_string(index=False))
    survivors = diag_df[(diag_df.gross_mo.abs() > 0.3) & diag_df.sign_stable & (diag_df.net_mo > 0)]
    print(f"\n  SURVIVORS (gross content, cost-surviving, sign-stable): "
          f"{list(survivors.signal) or 'NONE'}")

    print("\n" + "=" * 92)
    print("CORRELATION MATRIX — the property that actually matters")
    print("=" * 92)
    corr = df.corr()
    print(corr.round(2).to_string())

    avg_corr = (corr.values[np.triu_indices_from(corr.values, k=1)]).mean()
    print(f"\n  mean pairwise correlation: {avg_corr:+.3f}")

    print("\n" + "=" * 92)
    print("COMBINED (equal-weight all signals, then risk-parity weighted)")
    print("=" * 92)
    combo_eq = df.mean(axis=1)
    inv_vol = 1.0 / df.std()
    combo_rp = (df * inv_vol).sum(axis=1) / inv_vol.sum()
    best = pd.DataFrame(rows).sort_values("sharpe", ascending=False).head(6)["signal"].tolist()
    combo_best = df[best].mean(axis=1)

    out_rows = [
        summarize(combo_eq, f"COMBO equal-weight (all {len(df.columns)})"),
        summarize(combo_rp, "COMBO risk-parity (all)"),
        summarize(combo_best, f"COMBO top-6 by Sharpe {best}"),
    ]
    print(pd.DataFrame(out_rows).to_string(index=False))

    theoretical = np.mean([r["sharpe"] for r in rows]) * np.sqrt(len(df.columns))
    print(f"\n  mean individual Sharpe {np.mean([r['sharpe'] for r in rows]):+.3f}")
    print(f"  if perfectly uncorrelated, combined would be {theoretical:+.3f}")
    print(f"  actually achieved (equal weight)            {sharpe(combo_eq):+.3f}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "note": "survivorship-biased universe (currently-listed only) — upper bound",
        "params": vars(args),
        "signals": rows,
        "combined": out_rows,
        "mean_pairwise_corr": round(float(avg_corr), 4),
        "correlation_matrix": corr.round(4).to_dict(),
    }, indent=2, default=str))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
