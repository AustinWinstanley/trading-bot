"""Fundamental and insider signals — the independence test.

The price/volume library collapsed into ~1 usable signal because every signal
was a transformation of the same data. These signals come from different
generative processes — accounting statements and insider decisions — so their
correlation to momentum is a genuine empirical question, and it is the question
that decides whether combining buys anything.

Signals (each with its academic prior, specified before running):
  value        earnings yield: trailing-12m net income / market cap (Basu 1977)
  quality      gross profitability: gross profit / assets (Novy-Marx 2013)
  cashflow     operating cash flow / assets — harder to manufacture than EPS
  accruals     Sloan (1996): earnings not backed by cash mean-revert; the
               signal is -(NI - OCF)/assets
  insider_net  net open-market insider buying intensity over the trailing
               quarter (Seyhun; Cohen-Malloy-Pomorski for the routine split)

Point-in-time construction: a fact enters the panel on its FILED date + 1
trading day. Values forward-fill until the next filing (capped at 400 days so
a dead filer drops out rather than being carried at stale numbers forever).

The output of interest is the correlation matrix of these against mom_12_1 —
not the standalone Sharpes.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.signal_library import (
    SIGNALS as PRICE_SIGNALS,
    eligibility,
    neutral_portfolio,
    sharpe,
    summarize,
)
from backtest.xsec_data import load as xload
from engine.data import REPO_ROOT

FUND_PARQUET = next((REPO_ROOT / "state" / "fundamentals").glob("fundamentals_*.parquet"), None)
INSIDER_PARQUET = next((REPO_ROOT / "state" / "edgar").glob("insider_*.parquet"), None)
STALE_DAYS = 400
LAST_CIK_COVERAGE: dict | None = None


# --------------------------------------------------------------------------
# Panel construction: (dates x symbols) matrices from filings
# --------------------------------------------------------------------------


def _pivot_pit(facts: pd.DataFrame, dates: pd.DatetimeIndex, value_col: str = "value") -> pd.DataFrame:
    """Point-in-time matrix: last filed value per symbol, forward-filled.

    `facts` needs columns [symbol, avail_date, value]. Duplicate filings on the
    same day keep the last. Stale values (> STALE_DAYS old) become NaN.
    """
    f = facts.dropna(subset=["symbol", "avail_date", value_col])
    f = f.sort_values("avail_date").drop_duplicates(["symbol", "avail_date"], keep="last")
    wide = f.pivot(index="avail_date", columns="symbol", values=value_col)
    wide = wide.reindex(wide.index.union(dates)).ffill(limit_area=None)
    wide = wide.reindex(dates)

    # Staleness: NaN out any value whose last update is too old.
    last_update = f.pivot(index="avail_date", columns="symbol", values="avail_date")
    last_update = last_update.apply(pd.to_datetime)
    last_update = last_update.reindex(last_update.index.union(dates)).ffill().reindex(dates)
    age = last_update.rsub(pd.Series(dates, index=dates), axis=0).apply(lambda s: s.dt.days)
    return wide.where(age.le(STALE_DAYS))


def _trailing_4q_sum(quarterly: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol trailing-4-quarter sum from discrete quarterly (qtrs=1) facts.

    Keyed on each filing's own `avail_date`, so the figure updates every
    quarter as a new 10-Q lands, rather than sitting still for a year
    between 10-Ks. Requires 4 accumulated quarterly filings before it
    produces a value; callers should fall back to the annual (qtrs=4) figure
    until then.
    """
    q = quarterly.dropna(subset=["symbol", "avail_date", "value"])
    q = q.sort_values("avail_date").drop_duplicates(["symbol", "avail_date"], keep="last")
    if q.empty:
        return q.assign(value=q["value"])
    q["value"] = q.groupby("symbol")["value"].transform(
        lambda s: s.rolling(4, min_periods=4).sum()
    )
    return q.dropna(subset=["value"])[["symbol", "avail_date", "value"]]


def cik_map_coverage(df: pd.DataFrame, cmap: pd.DataFrame) -> dict:
    """How much of the fundamentals CIK population the current-only ticker
    map actually joins - see engine.fundamentals.cik_map's docstring.
    Computed fresh on every run so this doesn't silently drift stale."""
    all_ciks = set(df["cik"].dropna().astype("int64"))
    mapped_ciks = set(cmap["cik"].dropna().astype("int64"))
    matched = all_ciks & mapped_ciks
    return {
        "total_ciks_with_facts": len(all_ciks),
        "ciks_matched_to_a_current_ticker": len(matched),
        "coverage_pct": round(100 * len(matched) / len(all_ciks), 1) if all_ciks else 0.0,
        "note": (
            "cik_map() is current-listing only; delisted/deregistered CIKs "
            "are dropped, not mismapped. Every signal here inherits this "
            "survivorship bias in addition to the price panel's own."
        ),
    }


def load_fundamental_matrices(dates: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    from engine.fundamentals import cik_map

    if FUND_PARQUET is None:
        raise FileNotFoundError("run engine.fundamentals.build() first")
    df = pd.read_parquet(FUND_PARQUET)
    cmap = cik_map()
    global LAST_CIK_COVERAGE
    LAST_CIK_COVERAGE = cik_map_coverage(df, cmap)
    df = df.merge(cmap[["cik", "symbol"]], on="cik", how="inner")
    df["avail_date"] = (df["filed"] + pd.Timedelta(days=1)).dt.tz_localize("UTC").dt.normalize()

    # Trailing-12m flows: a rolling sum of the last 4 discrete quarterly
    # (qtrs=1) facts, updating every 10-Q — falling back to the annual
    # (qtrs=4) 10-K figure only before 4 quarterly filings have accumulated
    # for that symbol. The previous version only ever used the qtrs=4 rows,
    # so every flow figure (net income, OCF, gross profit) was up to a year
    # stale between 10-Ks despite fresher 10-Q data being available in the
    # same cache — the quality_momentum_filter_feasibility.json audit's
    # "prefers annual instead of true trailing four quarters" finding.
    out: dict[str, pd.DataFrame] = {}

    def matrix_for(tag: str, *, flow: bool) -> pd.DataFrame:
        sub = df[df["tag"] == tag]
        if flow:
            quarterly = sub[sub["qtrs"] == "1"][["symbol", "avail_date", "value"]]
            annual = sub[sub["qtrs"] == "4"][["symbol", "avail_date", "value"]]
            m_ttm = _pivot_pit(_trailing_4q_sum(quarterly), dates)
            m_annual = _pivot_pit(annual, dates)
            m = m_ttm.combine_first(m_annual)
        else:
            point = sub[sub["qtrs"] == "0"] if (sub["qtrs"] == "0").any() else sub
            m = _pivot_pit(point[["symbol", "avail_date", "value"]], dates)
        return m

    out["net_income"] = matrix_for("NetIncomeLoss", flow=True)
    out["ocf"] = matrix_for("NetCashProvidedByUsedInOperatingActivities", flow=True)
    out["gross_profit"] = matrix_for("GrossProfit", flow=True)
    out["assets"] = matrix_for("Assets", flow=False)
    out["equity"] = matrix_for("StockholdersEquity", flow=False)
    out["shares"] = matrix_for("CommonStockSharesOutstanding", flow=False)
    return out


def load_insider_matrix(dates: pd.DatetimeIndex, window: int = 63) -> pd.DataFrame:
    """Net insider open-market buying intensity, trailing `window` days."""
    from engine.edgar import open_market_buys

    if INSIDER_PARQUET is None:
        raise FileNotFoundError("insider parquet missing")
    ins = pd.read_parquet(INSIDER_PARQUET)
    ins["avail_date"] = (ins["filing_date"] + pd.Timedelta(days=1)).dt.tz_localize("UTC").dt.normalize()

    buys = open_market_buys(ins, min_value=10_000)
    sells = ins[(ins["code"] == "S") & (ins["acq_disp"] == "D")
                & (ins["is_officer"] | ins["is_director"])]

    def daily_sum(frame):
        g = frame.groupby(["avail_date", "symbol"])["value"].sum().unstack()
        return g.reindex(g.index.union(dates)).fillna(0.0).reindex(dates).fillna(0.0)

    b, s = daily_sum(buys), daily_sum(sells)
    assert b.values.sum() != 0, "insider buy matrix is all zero — date alignment broke again"
    cols = b.columns.union(s.columns)
    net = b.reindex(columns=cols, fill_value=0) - s.reindex(columns=cols, fill_value=0)
    rolled = net.rolling(window, min_periods=1).sum()
    # Log-scale dollars: a $10M buy is not 100x more informative than $100k.
    return np.sign(rolled) * np.log1p(rolled.abs())


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------


def build_signal_scores(close, volume) -> dict[str, pd.DataFrame]:
    dates = close.index
    f = load_fundamental_matrices(dates)
    mktcap = close * f["shares"].reindex(columns=close.columns)

    def A(m):  # align helper
        return m.reindex(columns=close.columns)

    ni, ocf = A(f["net_income"]), A(f["ocf"])
    gp, assets = A(f["gross_profit"]), A(f["assets"])

    scores: dict[str, pd.DataFrame] = {}
    scores["value"] = ni / mktcap.replace(0, np.nan)
    scores["quality"] = gp / assets.replace(0, np.nan)
    scores["cashflow"] = ocf / assets.replace(0, np.nan)
    scores["accruals"] = -(ni - ocf) / assets.replace(0, np.nan)
    scores["insider_net"] = load_insider_matrix(dates).reindex(columns=close.columns)
    return scores


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """Alpaca stamps bars at 04:00/05:00 UTC; filings land at midnight. Every
    matrix must share midnight-normalised dates or reindex() silently matches
    nothing — the insider signal came back all-zero exactly this way."""
    out = df.copy()
    out.index = pd.DatetimeIndex(out.index).normalize()
    return out[~out.index.duplicated(keep="last")]


def main() -> None:
    close, volume = xload()
    close, volume = _normalize_index(close), _normalize_index(volume)
    cls = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in cls["stocks"] if s in close.columns]
    close, volume = close[stocks], volume[stocks]
    elig = eligibility(close, volume)

    print("Building fundamental matrices (point-in-time on filed+1)...")
    scores = build_signal_scores(close, volume)
    for name, sc in scores.items():
        cov = sc.notna().sum(axis=1)
        print(f"  {name:12} median names covered/day: {int(cov.median()):,}")

    print("\nRunning market-neutral portfolios (monthly rebalance, 15bps)...")
    streams: dict[str, pd.Series] = {}
    rows = []
    for name, sc in scores.items():
        r = neutral_portfolio(sc, close, elig, rebalance=21, cost_bps=15).iloc[260:]
        g = neutral_portfolio(sc, close, elig, rebalance=21, cost_bps=0).iloc[260:]
        h = len(g) // 2
        streams[name] = r
        rows.append({**summarize(r, name),
                     "gross": round(sharpe(g), 3),
                     "oos_1st": round(sharpe(g.iloc[:h]), 3),
                     "oos_2nd": round(sharpe(g.iloc[h:]), 3),
                     "sign_stable": bool(np.sign(sharpe(g.iloc[:h])) == np.sign(sharpe(g.iloc[h:])))})

    # The incumbent to beat / diversify: momentum from the price library.
    mom = neutral_portfolio(PRICE_SIGNALS["mom_12_1"](close, volume), close, elig,
                            rebalance=21, cost_bps=15).iloc[260:]
    streams["mom_12_1"] = mom
    rows.append(summarize(mom, "mom_12_1"))

    pd.set_option("display.width", 220)
    print("\n" + "=" * 100)
    print("STANDALONE + DIAGNOSTICS")
    print("=" * 100)
    print(pd.DataFrame(rows).to_string(index=False))

    df = pd.DataFrame(streams).dropna()
    corr = df.corr()
    print("\n" + "=" * 100)
    print("CORRELATION MATRIX — the number this whole exercise is about")
    print("=" * 100)
    print(corr.round(2).to_string())

    print("\ncorrelation to mom_12_1:")
    print(corr["mom_12_1"].drop("mom_12_1").round(3).to_string())

    # Combination: survivors only (positive net Sharpe, sign-stable)
    diag = {r["signal"]: r for r in rows if "sign_stable" in r}
    keep = [n for n, d in diag.items() if d["sharpe"] > 0 and d["sign_stable"]]
    keep_all = keep + ["mom_12_1"]
    print(f"\nsurvivors for combination: {keep_all}")
    if len(keep_all) >= 2:
        combo = df[keep_all].mean(axis=1)
        print("\n" + "=" * 100)
        print("COMBINED")
        print("=" * 100)
        out = [summarize(df[n], n) for n in keep_all] + [summarize(combo, "COMBO equal-weight")]
        print(pd.DataFrame(out).to_string(index=False))

    print("\nCIK->ticker map coverage (see engine.fundamentals.cik_map docstring):")
    print(json.dumps(LAST_CIK_COVERAGE, indent=2))

    Path("reports/fund_signals.json").write_text(json.dumps({
        "standalone": rows,
        "correlation_to_momentum": corr["mom_12_1"].drop("mom_12_1").round(4).to_dict(),
        "correlation_matrix": corr.round(4).to_dict(),
        "cik_map_coverage": LAST_CIK_COVERAGE,
        "limitations": [
            "cik_map_coverage above: the current-only CIK->ticker map drops "
            "delisted/deregistered companies entirely (not mismapped) - "
            "verified 2026-08-03 at ~45% of the CIK population with "
            "fundamental facts. This compounds the price panel's own "
            "current-listing survivorship bias. Treat any positive result "
            "here as an unvalidated upper bound; a negative result is "
            "trustworthy since the bias runs in the signal's favor.",
            "Trailing-12m flow figures (net income, OCF, gross profit) use a "
            "rolling sum of the last 4 discrete quarterly (qtrs=1) facts, "
            "falling back to the annual (qtrs=4) 10-K figure only before 4 "
            "quarterly filings have accumulated. Point-type facts (assets, "
            "equity, shares) use the latest point-in-time value as filed.",
            "Point-in-time discipline: every fact is keyed on filed+1 "
            "trading day, not period end.",
        ],
    }, indent=2, default=str))
    print("\nWrote reports/fund_signals.json")


if __name__ == "__main__":
    main()
