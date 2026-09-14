from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.production_portfolio import returns_summary
from backtest.riskfree import RF_SYMBOL, load_rf_daily


def _write_bil(tmp_path: Path, closes: list[float]) -> Path:
    index = pd.date_range("2024-01-02", periods=len(closes), freq="B", tz="UTC")
    frame = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000] * len(closes),
    }, index=pd.DatetimeIndex(index, name="timestamp"))
    frame.to_parquet(tmp_path / f"{RF_SYMBOL}.parquet")
    return tmp_path


def test_daily_total_return_from_adjusted_close(tmp_path):
    history = _write_bil(tmp_path, [100.0, 100.02, 100.04, 100.05])
    rf = load_rf_daily(history)
    assert rf.name == "rf"
    assert rf.index.tz is None
    assert list(rf.index) == list(pd.date_range("2024-01-03", periods=3, freq="B"))
    np.testing.assert_allclose(
        rf.to_numpy(), [0.0002, 0.0002 / 1.0002, 0.0001 / 1.0004], rtol=1e-9
    )


def test_missing_cache_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_rf_daily(tmp_path)


def test_returns_summary_is_unchanged_without_rf_and_adds_excess_sharpe_with_it(tmp_path):
    history = _write_bil(tmp_path, [100.0 * (1 + 0.0002) ** i for i in range(300)])
    rf = load_rf_daily(history)
    index = pd.date_range("2024-01-03", periods=250, freq="B")  # inside rf coverage
    r = pd.Series(np.full(250, 0.0009), index=index)
    r.iloc[::2] = -0.0002
    plain = returns_summary(r, "x")
    with_rf = returns_summary(r, "x", rf=rf)
    assert {k: with_rf[k] for k in plain} == plain
    assert set(with_rf) - set(plain) == {"excess_sharpe"}
    # rf is a constant 2bp/day here, so the excess series is r - 0.0002 with
    # the same vol: the Sharpe drops by exactly 0.0002 * 252 / vol.
    expected = round(float((r - 0.0002).mean() * 252 / (r.std() * np.sqrt(252))), 3)
    assert with_rf["excess_sharpe"] == expected
    assert with_rf["excess_sharpe"] < with_rf["sharpe"]


def test_days_beyond_the_rf_series_count_as_zero(tmp_path):
    history = _write_bil(tmp_path, [100.0, 100.01, 100.02])
    rf = load_rf_daily(history)  # covers 2024-01-03 and 2024-01-04 only
    index = pd.date_range("2024-01-03", periods=100, freq="B")
    r = pd.Series(np.linspace(-0.002, 0.003, 100), index=index)
    with_rf = returns_summary(r, "x", rf=rf)
    excess = r.copy()
    excess.iloc[:2] -= rf.to_numpy()
    expected = round(float(excess.mean() * 252 / (excess.std() * np.sqrt(252))), 3)
    assert with_rf["excess_sharpe"] == expected


@pytest.mark.skipif(
    not Path("state/history/BIL.parquet").exists(), reason="BIL cache not present"
)
def test_cached_bil_looks_like_a_cash_rate():
    rf = load_rf_daily()
    assert rf.index.is_monotonic_increasing and rf.index.is_unique
    assert rf.index[0].year == 2007
    annualized = float(rf.mean() * 252)
    assert 0.0 < annualized < 0.06
    assert rf.abs().max() < 0.01  # a bill fund never moves a percent in a day
