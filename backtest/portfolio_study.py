"""Portfolio-level evaluation: correlation, not standalone performance.

Asking "does this strategy beat SPY on its own?" is the wrong question, and it
is the question the earlier studies in this repo answered. What determines
whether a strategy is worth holding is its *marginal contribution* to a
portfolio, which is driven by correlation. A strategy with a lower Sharpe than
SPY but near-zero correlation to it improves the portfolio; a strategy with a
higher Sharpe and 0.9 correlation barely does.

This module assembles the return streams from every strategy built in this repo
and evaluates them the right way: correlation matrix, equal-weight
combinations, and a vol-matched comparison that charges retail borrow on the
leverage needed to equalise risk.

    python -m backtest.portfolio_study
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.xsec_data import load as xload
from backtest.xsec_momentum import build_portfolio, operating_stock_symbols
from engine.cboe import series
from engine.tiingo import load_parquet

TRADING_DAYS = 252
BORROW_RATE = 0.05  # retail margin; institutions fund far cheaper, which is
                    # the single biggest structural disadvantage in this repo


def _norm(s: pd.Series) -> pd.Series:
    """Align indices to date. Tiingo stamps midnight UTC, Alpaca 04:00/05:00 —
    without this the series share no timestamps and every join is empty."""
    s = s.copy()
    s.index = pd.DatetimeIndex(s.index).tz_convert("UTC").normalize()
    return s


def sharpe(r: pd.Series) -> float:
    v = r.std() * np.sqrt(TRADING_DAYS)
    return float((r.mean() * TRADING_DAYS) / v) if v > 0 else 0.0


def max_dd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    return float(((eq - eq.cummax()) / eq.cummax()).min())


def build_streams() -> pd.DataFrame:
    spy = _norm(load_parquet(["SPY"], Path("state/history_deep"))["SPY"]["close"])
    put = _norm(series("PUT"))
    on = (spy > spy.rolling(200).mean()).shift(1).fillna(False)
    trend = spy.pct_change().fillna(0).where(on, 0.0)

    close, volume = xload()
    cls = json.loads(Path("state/universe_classified.json").read_text())
    stocks = operating_stock_symbols(
        cls["stocks"], close.columns, exclude={"SPY"}
    )
    c, v = close[stocks + ["SPY"]], volume[stocks + ["SPY"]]
    kw = dict(lookback=252, skip=21, top_n=20, rebalance=21, cost_bps=15)
    mom_long, _ = build_portfolio(c, v, **kw)
    mom_ls, _ = build_portfolio(c, v, short_bottom=True, **kw)

    df = pd.DataFrame({
        "SPY": spy.pct_change(),
        "PUT_write": put.pct_change(),
        "SPY_trend": trend,
        "Mom_long": _norm(mom_long).pct_change(),
        "Mom_LS": _norm(mom_ls).pct_change(),
    }).dropna()
    return df[(df != 0).any(axis=1)]


def vol_matched(r: pd.Series, target_vol: float, label: str) -> dict:
    """Lever to a target volatility, charging borrow on the excess.

    This is the fair way to compare a high-Sharpe/low-vol strategy against the
    index: Sharpe only converts into return if you can finance the leverage.
    """
    v = float(r.std() * np.sqrt(TRADING_DAYS))
    lev = target_vol / v if v > 0 else 1.0
    scaled = r * lev - (max(lev - 1, 0) * BORROW_RATE / TRADING_DAYS)
    return {
        "portfolio": label,
        "leverage": round(lev, 2),
        "ann_ret": round(float(scaled.mean() * TRADING_DAYS), 4),
        "vol": round(float(scaled.std() * np.sqrt(TRADING_DAYS)), 4),
        "sharpe": round(sharpe(scaled), 3),
        "max_dd": round(max_dd(scaled), 4),
    }


def main() -> None:
    s = build_streams()
    print(f"window {s.index[0].date()} -> {s.index[-1].date()} ({len(s):,} days)\n")

    print("STANDALONE")
    for c in s:
        print(f"  {c:10} sharpe {sharpe(s[c]):+.3f}  ann {float(s[c].mean()*TRADING_DAYS):+.2%}")

    print("\nCORRELATION MATRIX")
    print(s.corr().round(2).to_string())

    print("\nEQUAL-WEIGHT COMBINATIONS")
    combos = {
        "SPY alone": ["SPY"],
        "SPY + PUT": ["SPY", "PUT_write"],
        "SPY + Mom_LS": ["SPY", "Mom_LS"],
        "SPY + PUT + Mom_LS": ["SPY", "PUT_write", "Mom_LS"],
        "SPY + PUT + Mom_LS + trend": ["SPY", "PUT_write", "Mom_LS", "SPY_trend"],
    }
    rows = []
    for name, cols in combos.items():
        r = s[cols].mean(axis=1)
        rows.append({"portfolio": name, "sharpe": round(sharpe(r), 3),
                     "ann_ret": round(float(r.mean() * TRADING_DAYS), 4),
                     "vol": round(float(r.std() * np.sqrt(TRADING_DAYS)), 4),
                     "max_dd": round(max_dd(r), 4)})
    print(pd.DataFrame(rows).to_string(index=False))

    print("\nVOL-MATCHED TO SPY (borrow charged at 5%)")
    target = float(s["SPY"].std() * np.sqrt(TRADING_DAYS))
    combo = s[["SPY", "PUT_write", "Mom_LS", "SPY_trend"]].mean(axis=1)
    print(pd.DataFrame([
        vol_matched(s["SPY"], target, "SPY"),
        vol_matched(combo, target, "4-way equal weight"),
    ]).to_string(index=False))


if __name__ == "__main__":
    main()
