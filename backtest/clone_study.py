"""13F clone backtest: do top funds' filed holdings predict returns at a 45-day lag?

Construction, all point-in-time:
  * A fund's holdings become visible at FILING_DATE + 1 (usually ~45 days after
    quarter end).
  * Portfolio = each fund's TOP-N positions by filed value ("best ideas"),
    equal-weighted across (fund, position); rebalanced whenever new filings
    become visible (monthly check).
  * Consensus variant: names held in the top-N of 2+ funds.

Benchmarked against SPY over the identical window, plus correlation to the
existing combo. Universe is whatever the funds held, mapped CUSIP->ticker via
fails-to-deliver files; unmapped or unpriced holdings are dropped and counted.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.xsec_data import load as xload
from engine.thirteenf import FUNDS, build_holdings, cusip_ticker_map

TD = 252


def sh(r: pd.Series) -> float:
    v = r.std() * np.sqrt(TD)
    return float((r.mean() * TD) / v) if v > 0 else 0.0


def summarize(r: pd.Series, name: str) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    mx = eq.cummax()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    return {"portfolio": name,
            "cagr": round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4),
            "sharpe": round(sh(r), 3),
            "vol": round(float(r.std() * np.sqrt(TD)), 4),
            "max_dd": round(float(((eq - mx) / mx).min()), 4)}


def build_weights(holdings: pd.DataFrame, dates: pd.DatetimeIndex,
                  *, top_n: int = 10, min_funds: int = 1) -> pd.DataFrame:
    """Weight matrix from filings, forward-filled between filing events."""
    dates = pd.DatetimeIndex(dates)
    if dates.tz is not None:
        dates = dates.tz_convert("UTC").tz_localize(None)
    dates = dates.normalize()
    h = holdings.sort_values("filing_date")
    avail = pd.DatetimeIndex(h["filing_date"] + pd.Timedelta(days=1))
    if avail.tz is not None:
        avail = avail.tz_convert("UTC").tz_localize(None)
    h["avail"] = avail.normalize()

    # top-N per (fund, period)
    h = h.sort_values("value", ascending=False)
    h["rank"] = h.groupby(["cik", "period"]).cumcount() + 1
    top = h[h["rank"] <= top_n]

    events = sorted(top["avail"].unique())
    weights = {}
    for ev in events:
        # Latest filing per fund visible as of this event date.
        vis = top[top["avail"] <= ev]
        latest_period = vis.groupby("cik")["period"].max()
        cur = vis.merge(latest_period.rename("latest"), on="cik")
        cur = cur[cur["period"] == cur["latest"]]
        counts = cur.groupby("symbol")["cik"].nunique()
        names = counts[counts >= min_funds].index
        if len(names) == 0:
            continue
        w = pd.Series(1.0 / len(names), index=names)
        weights[ev] = w

    wdf = pd.DataFrame(weights).T
    wdf.index = pd.DatetimeIndex(wdf.index)
    # Each event row must be COMPLETE before forward-filling: a name absent
    # from the latest filings has weight 0 on that row. Leaving it NaN let
    # ffill carry stale weights forever, so names accumulated (319 "names"
    # from a 110-name cap), row sums grew to ~4-6x, and the portfolio was
    # silently levered — hence the fake 60% CAGR / 90% vol first run.
    wdf = wdf.fillna(0.0)
    wdf = wdf.reindex(wdf.index.union(dates)).ffill().reindex(dates).fillna(0.0)
    row_sums = wdf.sum(axis=1)
    assert float(row_sums.max()) < 1.0 + 1e-6, f"weights exceed 1.0: {row_sums.max()}"
    return wdf


def main() -> None:
    print("Loading holdings...")
    holdings = build_holdings(since_year=2021)
    cmap = cusip_ticker_map()
    holdings = holdings.merge(cmap, on="cusip", how="left")
    total = len(holdings)
    mapped = holdings["symbol"].notna().sum()
    print(f"  {total:,} positions, {mapped:,} mapped to tickers ({mapped/total:.0%})")
    holdings = holdings.dropna(subset=["symbol"])

    close, _ = xload()
    close = close.copy()
    close.index = pd.DatetimeIndex(close.index).tz_convert("UTC").tz_localize(None).normalize()
    close = close[~close.index.duplicated(keep="last")]
    rets = close.pct_change()

    in_px = holdings["symbol"].isin(close.columns)
    print(f"  {in_px.sum():,} positions have price data ({in_px.mean():.0%})")
    holdings = holdings[in_px]

    spy = rets["SPY"] if "SPY" in rets.columns else None
    results, streams = [], {}

    variants = [
        ("clone top10 any fund", dict(top_n=10, min_funds=1)),
        ("clone top5 any fund", dict(top_n=5, min_funds=1)),
        ("clone consensus 2+ funds", dict(top_n=10, min_funds=2)),
        ("clone conviction top3", dict(top_n=3, min_funds=1)),
    ]
    for name, kw in variants:
        w = build_weights(holdings, close.index, **kw)
        cols = [c for c in w.columns if c in rets.columns]
        r = (w[cols].shift(1) * rets[cols]).sum(axis=1)
        turnover = w[cols].diff().abs().sum(axis=1)
        r = (r - turnover * 15 / 10_000)
        r = r[w[cols].shift(1).sum(axis=1) > 0.5]     # only when actually invested
        streams[name] = r
        s = summarize(r, name)
        s["avg_names"] = int((w[cols] > 0).sum(axis=1).mean())
        results.append(s)
        print(f"  {name:26} sharpe {s['sharpe']:+.3f}  cagr {s['cagr']:+.2%}  names ~{s['avg_names']}")

    if spy is not None:
        common = streams[variants[0][0]].index
        results.append(summarize(spy.loc[common], "SPY same window"))

    pd.set_option("display.width", 160)
    print("\n" + "=" * 88)
    print(pd.DataFrame(results).to_string(index=False))

    if spy is not None:
        print("\ncorrelation to SPY:")
        for name, r in streams.items():
            j = pd.DataFrame({"s": spy, "c": r}).dropna()
            print(f"  {name:26} {float(j['s'].corr(j['c'])):+.3f}")

    Path("reports/clone_study.json").write_text(
        json.dumps(results, indent=2, default=str))
    print("\nWrote reports/clone_study.json")


if __name__ == "__main__":
    main()
