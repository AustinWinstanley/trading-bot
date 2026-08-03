"""Three untested return sources: asset-class trend, overnight drift, crypto.

Each has a genuine academic/structural prior and none is a transformation of
US-equity closes, so each is a candidate for real diversification against the
4-way combo. Priors, stated before running:

1. TSMOM (Moskowitz-Ooi-Pedersen 2012): sign of an asset's own trailing return
   predicts its next month across every asset class. The canonical managed-
   futures return stream, historically near-zero correlation to equities.
   Implementation here is long/flat (no shorts — Robinhood parity).

2. Overnight drift (Cooper-Cliff-Gulen 2008 and successors): virtually all of
   equity's return accrues close-to-open; intraday nets ~zero. Structural
   (earnings and news land off-hours). Tradeable only in size via MOC/MOO
   orders, so treat as information as much as a strategy.

3. Crypto trend: same TSMOM logic on BTC/ETH. Retail-dominated flow and no
   index-fund bid means trend has historically worked better there than in
   equities. 2021-2026 includes the -77% 2022 winter — a fair stress test.

Output: standalone stats per stream, then correlation against the existing
4-way combo, then the marginal Sharpe of adding each.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from engine.cboe import series
from engine.data import load_env
from engine.tiingo import load_parquet
from backtest.production_portfolio import require_history
from backtest.xsec_data import load as xload
from backtest.xsec_momentum import build_portfolio, operating_stock_symbols

TD = 252


def norm(s: pd.Series) -> pd.Series:
    s = s.copy()
    s.index = pd.DatetimeIndex(s.index).tz_convert("UTC").normalize()
    return s[~s.index.duplicated(keep="last")]


def sh(r: pd.Series) -> float:
    v = r.std() * np.sqrt(TD)
    return float((r.mean() * TD) / v) if v > 0 else 0.0


def summarize(r: pd.Series, name: str) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    mx = eq.cummax()
    return {"stream": name, "from": str(r.index[0].date()), "to": str(r.index[-1].date()),
            "ann_ret": round(float(r.mean() * TD), 4), "sharpe": round(sh(r), 3),
            "vol": round(float(r.std() * np.sqrt(TD)), 4),
            "max_dd": round(float(((eq - mx) / mx).min()), 4)}


# --------------------------------------------------------------------------
# 1. TSMOM across asset classes
# --------------------------------------------------------------------------


def tsmom_stream() -> pd.Series:
    universe = {
        # equity handled elsewhere — this sleeve is everything else
        "GLD": "gold", "SLV": "silver", "DBC": "commodities", "USO": "oil",
        "UNG": "natgas", "DBA": "agriculture", "TLT": "long bonds", "IEF": "7-10y",
        "LQD": "IG credit", "HYG": "HY credit", "EMB": "EM debt", "VNQ": "REITs",
        "UUP": "dollar", "FXE": "euro", "FXY": "yen",
    }
    frames = {}
    frames.update(load_parquet(list(universe), Path("state/history_assets")))
    deep = load_parquet(["GLD", "TLT", "SHY", "IEF"], Path("state/history_deep"))
    for k, v in deep.items():
        frames.setdefault(k, v)
    require_history(list(universe), frames, label="TSMOM frontier")

    closes = pd.DataFrame({k: norm(v["close"]) for k, v in frames.items() if len(v)})
    rets = closes.pct_change()

    # Long an asset when its own trailing 12m return is positive; else flat.
    # Inverse-vol weight the active longs so oil doesn't dominate bonds.
    sig = (closes.shift(1) / closes.shift(253) - 1).gt(0)
    ivol = 1.0 / (rets.rolling(63, min_periods=40).std() * np.sqrt(TD)).clip(lower=0.04)
    w = sig.astype(float) * ivol
    w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0).shift(1)
    port = (w * rets).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1)
    enough_history = closes.shift(252).notna().sum(axis=1) >= 8
    return (port - turnover * 8 / 10_000).where(enough_history).dropna()


# --------------------------------------------------------------------------
# 2. Overnight vs intraday
# --------------------------------------------------------------------------


def overnight_streams() -> dict[str, pd.Series]:
    out = {}
    for sym in ("SPY", "QQQ"):
        df = load_parquet([sym], Path("state/history_deep")).get(sym)
        if df is None or "open" not in df:
            continue
        o, c = norm(df["open"]), norm(df["close"])
        out[f"{sym}_overnight"] = (o / c.shift(1) - 1).dropna()   # close -> next open
        out[f"{sym}_intraday"] = (c / o - 1).dropna()             # open -> close
    return out


# --------------------------------------------------------------------------
# 3. Crypto trend
# --------------------------------------------------------------------------


def completed_crypto_prices(
    bars: list[dict],
    *,
    today: dt.date | None = None,
) -> pd.Series:
    """Build a close series without the still-forming current UTC day."""
    cutoff = today or dt.datetime.now(dt.timezone.utc).date()
    prices = pd.Series({
        pd.Timestamp(bar["t"]).normalize(): float(bar["c"])
        for bar in bars
    }).sort_index()
    return prices[prices.index.date < cutoff]


def crypto_streams() -> dict[str, pd.Series]:
    load_env()
    import os
    r = requests.get(
        "https://data.alpaca.markets/v1beta3/crypto/us/bars",
        params={"symbols": "BTC/USD,ETH/USD", "timeframe": "1Day",
                "start": "2021-01-01", "limit": 10000},
        headers={"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
                 "APCA-API-SECRET-KEY": os.environ["ALPACA_API_SECRET"]},
        timeout=60,
    )
    r.raise_for_status()
    out = {}
    for sym, bars in (r.json().get("bars") or {}).items():
        px = completed_crypto_prices(bars)
        ret = px.pct_change()
        name = sym.split("/")[0]
        out[f"{name}_hold"] = ret.dropna()
        on = (px > px.rolling(100, min_periods=80).mean()).shift(1).fillna(False)
        # Trend, long/flat, 10bps per flip (crypto spreads are wider).
        flips = on.astype(int).diff().abs().fillna(0)
        out[f"{name}_trend100"] = (ret.where(on, 0.0) - flips * 10 / 10_000).dropna()
    return out


# --------------------------------------------------------------------------
# Existing 4-way combo, for marginal comparison
# --------------------------------------------------------------------------


def existing_combo() -> pd.Series:
    spy = norm(load_parquet(["SPY"], Path("state/history_deep"))["SPY"]["close"])
    put = norm(series("PUT"))
    on = (spy > spy.rolling(200).mean()).shift(1).fillna(False)
    trend = spy.pct_change().fillna(0).where(on, 0.0)
    close, volume = xload()
    cls = json.loads(Path("state/universe_classified.json").read_text())
    stocks = operating_stock_symbols(
        cls["stocks"], close.columns, exclude={"SPY"}
    )
    mls, _ = build_portfolio(close[stocks + ["SPY"]], volume[stocks + ["SPY"]],
                             lookback=252, skip=21, top_n=20, rebalance=21,
                             cost_bps=15, short_bottom=True)
    df = pd.DataFrame({"SPY": spy.pct_change(), "PUT": put.pct_change(),
                       "TREND": trend, "MOM_LS": norm(mls).pct_change()}).dropna()
    df = df[(df != 0).any(axis=1)]
    return df.mean(axis=1)


def main() -> None:
    print("Building streams...")
    streams: dict[str, pd.Series] = {}
    streams["TSMOM_assets"] = tsmom_stream()
    streams.update(overnight_streams())
    streams.update(crypto_streams())
    combo = existing_combo()

    rows = [summarize(r, n) for n, r in streams.items()]
    rows.append(summarize(combo, "EXISTING 4-way combo"))
    pd.set_option("display.width", 180)
    print("\n" + "=" * 96)
    print("STANDALONE")
    print("=" * 96)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 96)
    print("CORRELATION TO THE EXISTING COMBO + MARGINAL VALUE OF ADDING 20%")
    print("=" * 96)
    add_rows = []
    for name, r in streams.items():
        j = pd.DataFrame({"combo": combo, "new": r}).dropna()
        if len(j) < 300:
            continue
        corr = float(j["combo"].corr(j["new"]))
        base = sh(j["combo"])
        mixed = sh(0.8 * j["combo"] + 0.2 * j["new"])
        add_rows.append({"stream": name, "corr_to_combo": round(corr, 3),
                         "combo_sharpe_alone": round(base, 3),
                         "sharpe_with_20pct": round(mixed, 3),
                         "marginal": round(mixed - base, 3),
                         "overlap_days": len(j)})
    df = pd.DataFrame(add_rows).sort_values("marginal", ascending=False)
    print(df.to_string(index=False))

    Path("reports/frontier_study.json").write_text(json.dumps(
        {"standalone": rows, "marginal": add_rows}, indent=2, default=str))
    print("\nWrote reports/frontier_study.json")


if __name__ == "__main__":
    main()
