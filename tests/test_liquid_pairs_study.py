import numpy as np
import pandas as pd

from backtest.liquid_pairs_study import pair_state


def test_pair_state_enters_and_time_exits():
    index = pd.date_range("2020-01-01", periods=100, freq="B")
    left = pd.Series(100.0, index=index)
    right = pd.Series(100.0, index=index)
    left.iloc[70:] = 120.0 + np.arange(30) * 0.01
    state, _ = pair_state(left, right)
    assert state.ne(0).any()
    first = state.ne(0).idxmax()
    assert state.loc[first] == -1
    assert state.loc[first:].eq(0).any()
