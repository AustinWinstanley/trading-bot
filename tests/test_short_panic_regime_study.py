import numpy as np
import pandas as pd

from backtest.short_panic_regime_study import panic_rebound_state


def test_panic_state_requires_decline_volatility_and_rebound():
    index = pd.date_range("2020-01-01", periods=320, freq="B")
    returns = pd.Series(0.0, index=index)
    returns.iloc[200:240] = -0.01
    returns.iloc[240:270] = np.where(np.arange(30) % 2, -0.03, 0.035)
    state = panic_rebound_state(returns)
    assert not state.iloc[:200].any()
    assert state.iloc[240:].any()


def test_low_volatility_drawdown_does_not_trigger():
    index = pd.date_range("2020-01-01", periods=320, freq="B")
    returns = pd.Series(0.0, index=index)
    returns.iloc[180:300] = -0.002
    assert not panic_rebound_state(returns).any()
