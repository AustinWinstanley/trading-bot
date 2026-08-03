import pandas as pd
import pytest

from backtest.options_strategy_study import replace_spy_core


def test_replacement_swaps_only_requested_spy_weight():
    index = pd.bdate_range("2026-01-01", periods=3)
    incumbent = pd.Series([0.01, -0.02, 0.03], index=index)
    spy = pd.Series([0.02, -0.04, 0.06], index=index)
    option = pd.Series([0.01, -0.01, 0.02], index=index)
    result = replace_spy_core(incumbent, spy, option, weight=0.20)
    expected = incumbent + 0.20 * (option - spy)
    pd.testing.assert_series_equal(result, expected)


def test_replacement_aligns_dates_and_rejects_excess_weight():
    incumbent = pd.Series(
        [0.01, 0.02], index=pd.bdate_range("2026-01-01", periods=2)
    )
    short = pd.Series(
        [0.03], index=pd.bdate_range("2026-01-02", periods=1)
    )
    result = replace_spy_core(incumbent, short, short, weight=0.20)
    assert len(result) == 1
    with pytest.raises(ValueError, match="replacement weight"):
        replace_spy_core(incumbent, short, short, weight=0.50)
