"""Target-weight construction for the paper portfolio.

Three sleeves, all long-only, all reproducible from data on this box:

  clone   top-N filed positions per fund from state/thirteenf/holdings.parquet
          (the "conviction" variant that tested best), equal weight
  tsmom   each asset-class ETF long iff its own trailing 12m return > 0,
          inverse-vol weighted within the sleeve; unlit assets stay cash
  trend   the trend symbol iff above its 200DMA, else cash

Weights are fractions of total equity and always sum to <= 1. Cash is the
residual and is an intentional position, not an error.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from engine.config import Config
from engine.data import AlpacaClient, sma


def clone_targets(cfg: Config, tradable: set[str]) -> dict[str, float]:
    from engine.thirteenf import build_holdings, cusip_ticker_map

    sleeve = cfg.sleeves_paper["sleeves"]["clone"]
    top_n = int(cfg.sleeves_paper["clone_top_n"])

    h = build_holdings()                     # cached parquet; refreshed weekly by cron
    h = h.merge(cusip_ticker_map(), on="cusip", how="inner")

    # Latest filed period per fund, top-N by value.
    latest = h.groupby("cik")["period"].max().rename("latest")
    cur = h.merge(latest, on="cik")
    cur = cur[cur["period"] == cur["latest"]]
    cur = cur.sort_values("value", ascending=False)
    cur["rank"] = cur.groupby("cik").cumcount() + 1
    picks = cur[cur["rank"] <= top_n]

    names = sorted(set(picks["symbol"]) & tradable)
    if not names:
        return {}
    w = sleeve / len(names)
    return {s: w for s in names}


def tsmom_targets(cfg: Config, bars: dict[str, pd.DataFrame]) -> dict[str, float]:
    p = cfg.sleeves_paper
    sleeve = p["sleeves"]["tsmom"]
    lookback = int(p["tsmom_lookback_days"])

    signals: dict[str, float] = {}
    for sym in p["tsmom_universe"]:
        df = bars.get(sym)
        if df is None or len(df) < lookback + 2:
            continue
        close = df["close"]
        mom = float(close.iloc[-2] / close.iloc[-lookback - 1] - 1)   # yesterday's info
        if mom <= 0:
            continue
        vol = float(close.pct_change().tail(63).std() * np.sqrt(252))
        signals[sym] = 1.0 / max(vol, 0.04)

    if not signals:
        return {}
    total = sum(signals.values())
    return {s: sleeve * v / total for s, v in signals.items()}


def trend_targets(cfg: Config, bars: dict[str, pd.DataFrame]) -> dict[str, float]:
    p = cfg.sleeves_paper
    sym = p["trend_symbol"]
    ma_days = int(p["trend_ma_days"])
    df = bars.get(sym)
    if df is None or len(df) < ma_days + 1:
        return {}
    close = df["close"]
    ma = float(sma(close, ma_days).iloc[-2])          # yesterday's MA, no peeking
    last = float(close.iloc[-2])
    return {sym: p["sleeves"]["trend"]} if last > ma else {}


def build_targets(cfg: Config, client: AlpacaClient) -> tuple[dict[str, float], dict]:
    """Combined symbol -> weight, plus per-sleeve diagnostics."""
    p = cfg.sleeves_paper
    lookback = int(p["tsmom_lookback_days"])
    need = sorted(set(p["tsmom_universe"]) | {p["trend_symbol"]})
    start = dt.date.today() - dt.timedelta(days=int(lookback * 1.9) + 30)
    bars = client.get_bars(need, start, dt.date.today())

    # Only clone names that are actually tradable and fractionable get weights.
    tradable: set[str] = set()
    from engine.thirteenf import build_holdings, cusip_ticker_map
    h = build_holdings().merge(cusip_ticker_map(), on="cusip", how="inner")
    for sym in sorted(set(h["symbol"])):
        try:
            a = client.get_asset(sym)
            if a.get("tradable") and a.get("fractionable"):
                tradable.add(sym)
        except Exception:
            continue

    sleeves = {
        "clone": clone_targets(cfg, tradable),
        "tsmom": tsmom_targets(cfg, bars),
        "trend": trend_targets(cfg, bars),
    }

    combined: dict[str, float] = {}
    origin: dict[str, str] = {}
    for name, tw in sleeves.items():
        for sym, w in tw.items():
            combined[sym] = combined.get(sym, 0.0) + w
            origin[sym] = f"{origin.get(sym, '')}+{name}".strip("+")

    total = sum(combined.values())
    assert total <= 1.0 + 1e-9, f"targets sum to {total:.4f} > 1"
    diag = {
        "as_of": dt.date.today().isoformat(),
        "sleeve_counts": {k: len(v) for k, v in sleeves.items()},
        "sleeve_weights": {k: round(sum(v.values()), 4) for k, v in sleeves.items()},
        "total_weight": round(total, 4),
        "cash_weight": round(1 - total, 4),
        "origin": origin,
    }
    return combined, diag
