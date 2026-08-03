import pandas as pd
import pytest

from backtest.overnight_cost_study import (
    break_even_cost_bps_per_leg,
    overnight_returns,
)


def sample_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"], utc=True)
    return pd.DataFrame(
        {
            "open": [100.0, 102.0, 101.0],
            "close": [101.0, 100.0, 103.0],
        },
        index=index,
    )


def test_overnight_uses_previous_close_without_lookahead():
    result = overnight_returns(sample_frame())
    assert result.iloc[0] == pytest.approx(102.0 / 101.0 - 1)
    assert result.iloc[1] == pytest.approx(101.0 / 100.0 - 1)


def test_cost_is_charged_on_both_legs():
    gross = overnight_returns(sample_frame())
    net = overnight_returns(sample_frame(), cost_bps_per_leg=2.0)
    assert (gross - net).tolist() == pytest.approx([0.0004, 0.0004])


def test_break_even_cost_offsets_mean_return():
    gross = overnight_returns(sample_frame())
    cost = break_even_cost_bps_per_leg(gross)
    net = overnight_returns(sample_frame(), cost_bps_per_leg=cost)
    assert net.mean() == pytest.approx(0.0, abs=1e-12)
