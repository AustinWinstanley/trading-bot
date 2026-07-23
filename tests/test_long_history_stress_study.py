import pandas as pd
import pytest

from backtest.long_history_stress_study import (
    parse_french_momentum,
    stress_window,
    volatility_scale,
)


def test_french_parser_skips_metadata_and_missing_values():
    parsed = parse_french_momentum(
        "metadata\n,Mom,\n20260102,1.25,\n20260105,-0.50,\n"
        "20260106,-99.99,\n"
    )
    assert parsed.loc[pd.Timestamp("2026-01-02")] == 0.0125
    assert parsed.loc[pd.Timestamp("2026-01-05")] == -0.005
    assert len(parsed) == 2


def test_volatility_scale_matches_target_volatility():
    index = pd.date_range("2026-01-01", periods=30)
    proxy = pd.Series([0.01, -0.01] * 15, index=index)
    target = proxy * 2.5
    scale, correlation = volatility_scale(proxy, target)
    assert scale == pytest.approx(2.5)
    assert correlation == pytest.approx(1.0)


def test_stress_window_includes_return_and_drawdown():
    index = pd.date_range("2026-01-01", periods=4)
    returns = pd.Series([0.10, -0.20, 0.05, 0.01], index=index)
    result = stress_window(
        returns, "2026-01-01", "2026-01-04", "test"
    )
    assert result["total_return"] == pytest.approx(-0.0668)
    assert result["max_drawdown"] == -0.20
    assert result["worst_day"] == -0.20
