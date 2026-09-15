import numpy as np
import pandas as pd
import pytest

from backtest.trend_leveraged_index_study import (
    BIL_INCEPTION,
    INSTRUMENTS,
    load_close,
    target_weights,
    trend_on,
)


def _frame(n: int = 320, ma_days: int = 20, bil_start: int | None = None) -> pd.DataFrame:
    """Index rises for the first half, then drops sharply below its SMA."""
    index = pd.date_range("2020-01-01", periods=n, freq="B")
    half = n // 2
    spy = np.concatenate([
        100 + np.arange(half, dtype=float),
        100 + half - 5 * np.arange(1, n - half + 1, dtype=float),
    ])
    close = pd.DataFrame({
        "SPY": spy,
        "SSO": spy * 2,
        "BIL": np.full(n, 91.0),
    }, index=index)
    if bil_start is not None:
        close.loc[close.index[:bil_start], "BIL"] = np.nan
    return close


def test_registration_grid_is_the_module_grid():
    assert INSTRUMENTS == {"SPY": ["SSO", "UPRO"], "QQQ": ["QLD", "TQQQ"]}


def test_uses_previous_day_close_and_sma_no_look_ahead():
    close = _frame(ma_days=20)
    on = trend_on(close["SPY"], 20)
    prior = close["SPY"].shift(1)
    sma_prior = prior.rolling(20, min_periods=20).mean()
    expected = (prior > sma_prior) & sma_prior.notna()
    pd.testing.assert_series_equal(on, expected, check_names=False)
    # Warm-up: the first ma_days rows have no complete t-1 SMA -> OFF.
    assert not on.iloc[:20].any()
    assert on.iloc[20]  # first day with a full t-1 SMA on a rising series

    # The day the index first closes below its SMA, today's close is not
    # used: the sleeve stays ON that day and switches the NEXT day.
    weights = target_weights(close, index="SPY", vehicle="SSO", ma_days=20)
    same_day = (close["SPY"] > close["SPY"].rolling(20).mean()) & close["SPY"].rolling(20).mean().notna()
    first_break = same_day.iloc[20:].idxmin()  # first False after warm-up
    pos = close.index.get_loc(first_break)
    assert weights.loc[first_break, "SSO"] == 1.0
    assert weights.iloc[pos + 1]["SSO"] == 0.0
    assert weights.iloc[pos + 1]["BIL"] == 1.0

    # Tampering with tomorrow's close never changes today's weight.
    tampered = close.copy()
    tampered.loc[tampered.index[pos + 1]:, "SPY"] = 1e6
    w2 = target_weights(tampered, index="SPY", vehicle="SSO", ma_days=20)
    pd.testing.assert_frame_equal(w2.iloc[: pos + 1], weights.iloc[: pos + 1])


def test_switches_to_off_vehicle_below_the_sma():
    close = _frame(ma_days=20)
    weights = target_weights(close, index="SPY", vehicle="SSO", ma_days=20)
    assert list(weights.columns) == ["SSO", "BIL"]
    on_rows = weights["SSO"] == 1.0
    assert on_rows.any() and (~on_rows).any()
    assert (weights.loc[on_rows, "BIL"] == 0.0).all()
    # After the warm-up, every OFF day is 100% BIL.
    off_after_warmup = (~on_rows) & (weights.index > weights.index[20])
    assert (weights.loc[off_after_warmup, "BIL"] == 1.0).all()
    # The tail of the series is a crash: OFF and in BIL.
    assert weights["BIL"].iloc[-1] == 1.0 and weights["SSO"].iloc[-1] == 0.0


def test_cash_before_bil_inception_and_before_vehicle_listing():
    close = _frame(ma_days=20, bil_start=30)
    close.loc[close.index[:25], "SSO"] = np.nan
    weights = target_weights(close, index="SPY", vehicle="SSO", ma_days=20)
    # Rows where BIL has no bar and the trend is OFF are cash, not BIL.
    no_bil = close["BIL"].isna()
    assert (weights.loc[no_bil, "BIL"] == 0.0).all()
    # Rows where the vehicle has no bar are cash even if the trend is ON.
    no_vehicle = close["SSO"].isna()
    assert (weights.loc[no_vehicle, "SSO"] == 0.0).all()
    assert weights.iloc[:20].sum().sum() == 0.0  # warm-up + pre-inception


def test_weights_never_exceed_one_and_are_non_negative():
    close = _frame(ma_days=20, bil_start=30)
    weights = target_weights(close, index="SPY", vehicle="SSO", ma_days=20)
    assert (weights >= 0).all().all()
    assert (weights <= 1.0).all().all()
    assert (weights.sum(axis=1) <= 1.0 + 1e-12).all()
    assert set(weights.sum(axis=1).round(12).unique()) <= {0.0, 1.0}


def test_load_close_refuses_the_frozen_window():
    with pytest.raises(ValueError):
        load_close(["SPY"], end="2026-08-13")


def test_bil_inception_constant_matches_registration_note():
    assert BIL_INCEPTION == "2007-05-30"
