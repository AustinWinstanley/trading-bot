"""Production-equivalent four-sleeve portfolio backtest.

This evaluates the deployed target mix rather than combining headline metrics:

  clone  40%  13F conviction top-3, available filing_date + 1
  tsmom  25%  12-month long/flat asset trend, inverse-vol weighted
  trend  20%  SPY above 200DMA, else cash
  mom_ls 15% long + 15% short, weekly 12-1 momentum ranks

The cross-sectional universe is currently-listed companies, so clone and
MOM_LS results retain survivorship bias. Positive results are an upper bound.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.clone_study import build_weights
from backtest.xsec_data import load as load_xsec
from backtest.xsec_momentum import build_portfolio
from engine.config import load_config
from engine.thirteenf import build_holdings, cusip_ticker_map
from engine.tiingo import load_parquet

TD = 252
SHORT_BORROW = 0.03
MARGIN_RATE = 0.05


def require_history(
    symbols: list[str],
    frames: dict[str, pd.DataFrame],
    *,
    label: str,
) -> None:
    missing = sorted(set(symbols) - set(frames))
    if missing:
        raise FileNotFoundError(
            f"{label} research cache missing configured symbols: {missing}"
        )


def norm_index(obj: pd.Series | pd.DataFrame):
    out = obj.copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    out.index = idx.normalize()
    return out[~out.index.duplicated(keep="last")]


def returns_summary(r: pd.Series, label: str) -> dict:
    r = r.dropna()
    equity = (1 + r).cumprod()
    years = (r.index[-1] - r.index[0]).days / 365.25
    vol = float(r.std() * np.sqrt(TD))
    downside = float(r[r < 0].std() * np.sqrt(TD))
    return {
        "portfolio": label,
        "from": r.index[0].date().isoformat(),
        "to": r.index[-1].date().isoformat(),
        "years": round(years, 2),
        "cagr": round(float(equity.iloc[-1] ** (1 / years) - 1), 4),
        "ann_return": round(float(r.mean() * TD), 4),
        "vol": round(vol, 4),
        "sharpe": round(float(r.mean() * TD / vol), 3) if vol else 0.0,
        "sortino": round(float(r.mean() * TD / downside), 3) if downside else 0.0,
        "max_dd": round(float((equity / equity.cummax() - 1).min()), 4),
        "x_money": round(float(equity.iloc[-1]), 3),
    }


def trend_stream(spy: pd.Series, ma_days: int = 200) -> pd.Series:
    """Build the trend return before trimming to another sleeve's history.

    Computing the moving average after a short-history reindex creates a false
    warm-up cash period even when the underlying SPY history is available.
    """
    spy = norm_index(spy)
    spy_return = spy.pct_change(fill_method=None)
    trend_on = (
        spy.shift(1) > spy.shift(1).rolling(ma_days, min_periods=ma_days).mean()
    ).astype(float)
    return trend_on * spy_return


def clone_stream(close: pd.DataFrame) -> pd.Series:
    holdings = build_holdings().merge(cusip_ticker_map(), on="cusip", how="inner")
    holdings = holdings[holdings["symbol"].isin(close.columns)]
    weights = build_weights(holdings, close.index, top_n=3, min_funds=1)
    cols = weights.columns.intersection(close.columns)
    rets = close[cols].pct_change()
    gross = (weights[cols].shift(1) * rets).sum(axis=1)
    turnover = weights[cols].diff().abs().sum(axis=1)
    return gross - turnover * 15 / 10_000


def tsmom_stream() -> pd.Series:
    cfg = load_config()
    symbols = cfg.sleeves_paper["tsmom_universe"]
    frames = load_parquet(symbols, Path("state/history_assets"))
    require_history(symbols, frames, label="TSMOM")
    close = pd.DataFrame({
        symbol: norm_index(frame["close"])
        for symbol, frame in frames.items()
    })
    rets = close.pct_change()
    signal = (close / close.shift(252) - 1).gt(0)
    inverse_vol = 1 / (
        rets.rolling(63, min_periods=40).std() * np.sqrt(TD)
    ).clip(lower=0.04)
    weights = signal * inverse_vol
    weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    weights = weights.shift(1)
    gross = (weights * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    return gross - turnover * 8 / 10_000


def build_streams() -> pd.DataFrame:
    close_all, volume_all = load_xsec()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in classified["stocks"] if s in close_all.columns]
    close, volume = close_all[stocks], volume_all[stocks]

    spy_frame = load_parquet(["SPY"])["SPY"]
    spy_history = norm_index(spy_frame["close"])
    trend = trend_stream(spy_history).reindex(close.index)
    spy = spy_history.reindex(close.index).ffill()
    spy_ret = spy.pct_change()

    clone = clone_stream(close_all)
    mom_equity, _ = build_portfolio(
        close, volume, lookback=252, skip=21, top_n=20, rebalance=5,
        min_price=5.0, min_dollar_volume=5e6, cost_bps=15,
        short_bottom=True,
    )
    mom_ls = mom_equity.pct_change()
    return pd.DataFrame({
        "clone": clone,
        "tsmom": norm_index(tsmom_stream()),
        "trend": trend,
        "mom_ls": mom_ls,
        "spy": spy_ret,
    }).dropna()


def main() -> None:
    streams = build_streams()
    # build_portfolio is 50% long / 50% short (100% gross). Multiplying by
    # 0.30 produces the deployed 15% long + 15% short sleeve.
    legacy_raw = (
        0.40 * streams["clone"]
        + 0.25 * streams["tsmom"]
        + 0.20 * streams["trend"]
        + 0.30 * streams["mom_ls"]
    )
    raw = (
        0.40 * streams["spy"]
        + 0.25 * streams["tsmom"]
        + 0.20 * streams["trend"]
        + 0.30 * streams["mom_ls"]
    )
    base = raw - (0.15 * SHORT_BORROW / TD)
    levered = 2 * raw - (MARGIN_RATE / TD) - (0.30 * SHORT_BORROW / TD)
    legacy = legacy_raw - (0.15 * SHORT_BORROW / TD)

    results = [
        returns_summary(base, "production SPY-core base (1.15x gross)"),
        returns_summary(levered, "production SPY-core 2x (2.30x gross)"),
        returns_summary(legacy, "invalidated clone allocation (legacy)"),
        returns_summary(streams["spy"], "SPY buy & hold"),
    ]
    yearly = pd.DataFrame({
        "base": base, "2x": levered, "SPY": streams["spy"]
    }).groupby(lambda d: d.year).apply(lambda x: (1 + x).prod() - 1)

    payload = {
        "note": (
            "Currently-listed cross-sectional universe: survivorship-biased "
            "upper bound. Includes 15bps cross-sectional/clone costs, 8bps "
            "TSMOM costs, 3% short borrow, and 5% 2x margin financing."
        ),
        "results": results,
        "yearly_returns": {
            str(year): {k: round(float(v), 6) for k, v in row.items()}
            for year, row in yearly.iterrows()
        },
        "correlations": streams.corr().round(4).to_dict(),
    }
    out = Path("reports/production_portfolio.json")
    out.write_text(json.dumps(payload, indent=2))
    print(pd.DataFrame(results).to_string(index=False))
    print("\nYEARLY\n", yearly.round(3).to_string())
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
