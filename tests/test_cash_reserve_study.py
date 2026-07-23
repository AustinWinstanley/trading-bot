import pandas as pd

from backtest.cash_reserve_study import reserve_return


def test_reserve_is_invested_only_when_trend_is_off():
    index = pd.date_range("2020-01-01", periods=240, freq="B")
    spy = pd.Series(
        [100 + i for i in range(210)] + [309 - 8 * i for i in range(30)],
        index=index,
    )
    shy = pd.Series(range(100, 340), index=index, dtype=float)
    _, weight = reserve_return(spy, shy)
    assert weight.iloc[:200].eq(0).all()
    assert weight.iloc[-1] == 0.20
