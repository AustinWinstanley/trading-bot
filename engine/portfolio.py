"""Target-weight construction for the paper portfolio.

Four sleeves, all reproducible from data on this box (shorts enabled
2026-07-23 by user authorization for the Alpaca path):

  equity_core  unconditional SPY allocation
  tsmom   each asset-class ETF long iff its own trailing 12m return > 0,
          inverse-vol weighted within the sleeve; unlit assets stay cash
  trend   the trend symbol iff above its 200DMA, else cash
  mom_ls  market-neutral momentum: +w long ranks, -w short ranks, from the
          weekly-built target file; stands down if the file is stale

Weights are fractions of total equity and always sum to <= 1. Cash is the
residual and is an intentional position, not an error.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from engine.config import Config
from engine.data import AlpacaClient, sma


def equity_core_targets(cfg: Config) -> dict[str, float]:
    sleeve = float(cfg.sleeves_paper["sleeves"].get("equity_core", 0.0))
    return {"SPY": sleeve} if sleeve > 0 else {}


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


def tsmom_targets(
    cfg: Config,
    bars: dict[str, pd.DataFrame],
    held: frozenset[str] = frozenset(),
) -> dict[str, float]:
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
    targets = {s: sleeve * v / total for s, v in signals.items()}

    # Liquidity floor, using completed bars, after normalization. Dropping a
    # NOT-currently-held symbol here leaves its allocation in cash — the gate
    # rechecks the same floor independently at order time (on a window that
    # includes today's partial bar, so it can be marginally stricter; this is
    # a screen, not a guarantee of a fill either way) and simply rejects the
    # buy, which is inert.
    #
    # A symbol already HELD is never dropped here. Removing it from targets
    # would make the daily diff read "target 0" against a real position, and
    # engine/risk.py lets every sell/cover through unconditionally (closing
    # risk is never blocked) — so dropping a held name is not "stays in cash
    # until liquidity returns", it is an unconditional forced exit that the
    # gate cannot refuse, on a signal that only measures liquidity, not
    # whether the position should be closed. That is a real strategy change
    # (an exit rule) that no study has evaluated; keeping the target intact
    # for held names preserves the pre-existing behavior (the gate can still
    # reject a *buy* on this symbol, but a *sell* must come from an actual
    # signal decision, not a liquidity dip).
    floor = float(p.get("tsmom_min_dollar_volume", 0.0))
    for sym in list(targets):
        if sym in held:
            continue
        df = bars[sym]
        if "volume" not in df or len(df) < 21:
            continue
        completed = df.iloc[:-1].tail(20)
        avg_dollar_volume = float((completed["close"] * completed["volume"]).mean())
        if not np.isfinite(avg_dollar_volume) or avg_dollar_volume < floor:
            targets.pop(sym)
    return targets


def trend_targets(cfg: Config, bars: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Trend-following weight in trend_symbol when it's above its moving
    average; otherwise idle cash, UNLESS trend_reserve_symbol is configured
    (2x-lab only, see reports/cash_reserve_study.json) — then the reserve
    is invested in that near-zero-duration instrument instead of sitting
    at 0%. Falls back to idle cash (not the reserve) if the reserve
    symbol's own bars are unavailable, matching the same
    no-data-means-no-position discipline as everywhere else in this file.
    """
    p = cfg.sleeves_paper
    sym = p["trend_symbol"]
    ma_days = int(p["trend_ma_days"])
    df = bars.get(sym)
    if df is None or len(df) < ma_days + 1:
        return {}
    close = df["close"]
    ma = float(sma(close, ma_days).iloc[-2])          # yesterday's MA, no peeking
    last = float(close.iloc[-2])
    weight = p["sleeves"]["trend"]
    if last > ma:
        return {sym: weight}
    reserve_symbol = p.get("trend_reserve_symbol")
    if reserve_symbol and bars.get(reserve_symbol) is not None:
        return {reserve_symbol: weight}
    return {}


def mom_ls_targets(cfg: Config) -> dict[str, float]:
    """Market-neutral momentum from the weekly-built target file.

    Long names get +w, short names get -w. If the file is missing or stale
    the sleeve STANDS DOWN (returns {}) rather than trading on old ranks —
    a silent no-position is recoverable; trading stale shorts is not.
    """
    import json as _json
    from engine.data import REPO_ROOT as _ROOT

    p = cfg.sleeves_paper
    path = _ROOT / p["mom_ls_targets_file"]
    if not path.exists():
        return {}
    data = _json.loads(path.read_text())
    age = (dt.date.today() - dt.date.fromisoformat(data["as_of"])).days
    if age > int(p["mom_ls_max_age_days"]):
        return {}
    sleeve = p["sleeves"].get("mom_ls", 0.0)
    longs, shorts = data.get("long", []), data.get("short", [])
    out: dict[str, float] = {}
    if longs:
        w = sleeve / len(longs)
        for sym in longs:
            out[sym] = out.get(sym, 0.0) + w
    if shorts:
        w = sleeve / len(shorts)
        for sym in shorts:
            out[sym] = out.get(sym, 0.0) - w
    return out


def build_targets(
    cfg: Config,
    client: AlpacaClient,
    held: frozenset[str] = frozenset(),
) -> tuple[dict[str, float], dict]:
    """Combined symbol -> weight, plus per-sleeve diagnostics.

    `held` is the set of symbols currently in the broker account. It is used
    only to exempt already-held TSMOM positions from the liquidity-floor
    drop — see tsmom_targets' docstring/comment. An empty default keeps
    every other caller (tests, studies with no live positions) unaffected.
    """
    p = cfg.sleeves_paper
    lookback = int(p["tsmom_lookback_days"])
    need = set(p["tsmom_universe"]) | {p["trend_symbol"]}
    if p.get("trend_reserve_symbol"):
        need.add(p["trend_reserve_symbol"])
    need = sorted(need)
    start = dt.date.today() - dt.timedelta(days=int(lookback * 1.9) + 30)
    bars = client.get_bars(need, start, dt.date.today())

    tradable: set[str] = set()
    clone_allocation = float(p["sleeves"].get("clone", 0.0))
    if clone_allocation > 0:
        # Legacy/experimental clone support. Do not make these ~1,000 broker
        # asset calls when the production allocation is zero.
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
        "equity_core": equity_core_targets(cfg),
        "tsmom": tsmom_targets(cfg, bars, held),
        "trend": trend_targets(cfg, bars),
        "mom_ls": mom_ls_targets(cfg),
    }
    if clone_allocation > 0:
        sleeves["clone"] = clone_targets(cfg, tradable)

    lev = float(p.get("gross_leverage", 1.0))
    combined: dict[str, float] = {}
    origin: dict[str, str] = {}
    for name, tw in sleeves.items():
        for sym, w in tw.items():
            combined[sym] = combined.get(sym, 0.0) + w * lev
            origin[sym] = f"{origin.get(sym, '')}+{name}".strip("+")

    long_total = sum(w for w in combined.values() if w > 0)
    short_total = sum(-w for w in combined.values() if w < 0)
    assert long_total <= lev + 1e-9, f"long targets sum to {long_total:.4f} > leverage {lev}"
    assert short_total <= cfg.risk.max_short_exposure_pct + 1e-9, \
        f"short targets {short_total:.4f} exceed cap {cfg.risk.max_short_exposure_pct}"
    diag = {
        "as_of": dt.date.today().isoformat(),
        "sleeve_counts": {k: len(v) for k, v in sleeves.items()},
        "sleeve_weights": {k: round(sum(v.values()), 4) for k, v in sleeves.items()},
        # Scaled symbol detail is journaled for exposure attribution only. The
        # combined targets above remain the sole trading input.
        "sleeve_targets": {
            name: {sym: round(w * lev, 8) for sym, w in weights.items()}
            for name, weights in sleeves.items()
        },
        "long_weight": round(long_total, 4),
        "short_weight": round(short_total, 4),
        "total_weight": round(long_total, 4),
        "gross_leverage": lev,
        "cash_weight": round(1 - long_total, 4),
        "origin": origin,
    }
    return combined, diag
