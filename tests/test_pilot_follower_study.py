import pandas as pd
import pytest

from backtest.pilot_follower_study import (
    confirmation_mask,
    followed_weights,
)


def test_long_and_short_require_directionally_positive_markout():
    index = pd.bdate_range("2026-01-01", periods=4)
    close = pd.DataFrame({
        "LONG": [100.0, 101.0, 102.0, 103.0],
        "SHORT": [100.0, 99.0, 98.0, 97.0],
        "LOSER": [100.0, 99.0, 98.0, 97.0],
    }, index=index)
    scout = pd.DataFrame({
        "LONG": [0.5] * 4,
        "SHORT": [-0.5] * 4,
        "LOSER": [0.5] * 4,
    }, index=index)
    mask = confirmation_mask(
        close, scout, delay_sessions=1, require_positive=True
    )
    assert not mask.iloc[0].any()
    assert mask.loc[index[1], "LONG"]
    assert mask.loc[index[1], "SHORT"]
    assert not mask["LOSER"].any()


def test_follower_exits_immediately_and_never_flips_without_scout():
    index = pd.bdate_range("2026-01-01", periods=3)
    follower = pd.DataFrame({"A": [0.5, 0.5, -0.5]}, index=index)
    scout = pd.DataFrame({"A": [0.5, 0.0, 0.0]}, index=index)
    eligible = pd.DataFrame({"A": [True, True, True]}, index=index)
    result = followed_weights(follower, scout, eligible)
    assert result["A"].tolist() == [0.5, 0.0, 0.0]


def test_confirmation_delay_must_be_positive():
    frame = pd.DataFrame({"A": [1.0, 2.0]})
    with pytest.raises(ValueError, match="delay_sessions"):
        confirmation_mask(
            frame, frame, delay_sessions=0, require_positive=True
        )
