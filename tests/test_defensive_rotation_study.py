import pandas as pd
import pytest

from backtest.defensive_rotation_study import defensive_trend_stream


def prices(spy_values, *, periods=260):
    index = pd.bdate_range("2025-01-01", periods=periods)
    spy = pd.Series(spy_values, index=index)
    return pd.DataFrame({
        "SPY": spy,
        "IEF": pd.Series(range(100, 100 + periods), index=index),
        "TLT": pd.Series(range(100, 100 + periods), index=index),
        "GLD": pd.Series(range(100, 100 + periods), index=index),
    })


def test_cash_variant_has_no_return_when_spy_is_below_average():
    close = prices(list(range(400, 140, -1)))
    result = defensive_trend_stream(close, "cash", cost_bps=0)
    assert not result.empty
    assert (result == 0).all()


def test_fixed_fallback_uses_prior_day_signal_without_lookahead():
    close = prices(list(range(400, 140, -1)))
    result = defensive_trend_stream(close, "GLD", cost_bps=0)
    expected = close["GLD"].pct_change().reindex(result.index)
    pd.testing.assert_series_equal(result, expected, check_names=False)


def test_dual_momentum_owns_positive_risk_off_winner():
    close = prices(list(range(500, 240, -1)))
    close["IEF"] = 100.0
    close["TLT"] = 100.0
    close["GLD"] = pd.Series(range(100, 360), index=close.index, dtype=float)
    result = defensive_trend_stream(
        close, "12m dual momentum", cost_bps=0
    )
    assert len(result) == 7
    expected = close["GLD"].pct_change().reindex(result.index)
    pd.testing.assert_series_equal(result, expected, check_names=False)


def test_rejects_missing_prices_and_unknown_variant():
    close = prices(list(range(100, 360)))
    with pytest.raises(ValueError, match="missing defensive"):
        defensive_trend_stream(close.drop(columns="TLT"), "cash")
    with pytest.raises(ValueError, match="unknown defensive"):
        defensive_trend_stream(close, "not-a-strategy")
