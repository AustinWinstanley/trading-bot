"""Daily risk-free return series for excess-return statistics.

`returns_summary`'s `sharpe` is computed against rf = 0, which flatters every
long-biased stream in a 4-5% cash-rate regime and makes "beat SPY on Sharpe"
comparisons depend on how much idle cash each candidate holds. Pass
`load_rf_daily()` as `returns_summary(..., rf=...)` to get an additional
`excess_sharpe` computed on r - rf; the rf = 0 `sharpe` is left untouched so
every existing report stays comparable.

Source and caveats
------------------
The series is the daily total return of BIL (SPDR 1-3 Month T-Bill ETF) from
`state/history/BIL.parquet` — Tiingo daily bars whose `close` column is the
dividend-adjusted close, so monthly distributions are already compounded into
the price path. That makes it a *tradeable* cash proxy, which is exactly what
the paper accounts can hold (the 2x lab parks idle trend cash in BIL), but it
is not the textbook risk-free rate:

- BIL charges a 0.1356% expense ratio, so it UNDERSTATES the T-bill yield by
  roughly 13-14 bp/yr. Excess Sharpes computed against it are therefore very
  slightly generous to every candidate, uniformly.
- It holds 1-3 month bills, not the overnight rate; the difference is a few
  basis points in normal curves and can invert around FOMC pivots.
- Adjusted-close returns are lumpy around ex-dividend dates only if the
  adjustment is imperfect; Tiingo's series is smooth in practice, but a
  monthly "step" in rf is a data artefact, not a rate change.

The parquet ends whenever the last Tiingo refresh ran. `returns_summary`
fills days beyond the series with 0.0 rather than extrapolating, so a study
whose window outruns the cache carries a small, documented understatement
of rf at its tail — check `load_rf_daily().index[-1]` against the window.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from engine.tiingo import load_parquet

RF_SYMBOL = "BIL"
BIL_EXPENSE_RATIO = 0.001356


def load_rf_daily(history_dir: Path | None = None) -> pd.Series:
    """Daily BIL total return, tz-naive normalized DatetimeIndex, named "rf".

    Raises FileNotFoundError when the BIL bars are not cached — a study
    should fail loudly rather than silently fall back to rf = 0.
    """
    frames = load_parquet([RF_SYMBOL], history_dir)
    if RF_SYMBOL not in frames:
        where = history_dir or Path("state/history")
        raise FileNotFoundError(f"{where / (RF_SYMBOL + '.parquet')} is missing")
    close = frames[RF_SYMBOL]["close"].astype(float)
    idx = pd.DatetimeIndex(close.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    close.index = idx.normalize()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    rf = close.pct_change(fill_method=None).dropna()
    rf.name = "rf"
    return rf
