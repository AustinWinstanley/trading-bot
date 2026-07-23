import pandas as pd
import pytest

from backtest.capital_split_study import blend_profiles


def test_equal_split_is_daily_average_of_account_returns():
    index = pd.date_range("2026-01-01", periods=3)
    base = pd.Series([0.01, -0.02, 0.03], index=index)
    two_x = pd.Series([0.02, -0.04, 0.06], index=index)
    result = blend_profiles(base, two_x, two_x_weight=0.50)
    pd.testing.assert_series_equal(result, (base + two_x) / 2)


def test_split_aligns_dates_and_validates_weight():
    base = pd.Series([0.01, 0.02], index=pd.date_range("2026-01-01", periods=2))
    two_x = pd.Series([0.03], index=pd.date_range("2026-01-02", periods=1))
    result = blend_profiles(base, two_x, two_x_weight=0.25)
    assert len(result) == 1
    with pytest.raises(ValueError):
        blend_profiles(base, two_x, two_x_weight=1.1)
