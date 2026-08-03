import pandas as pd

from backtest.xsec_momentum import operating_stock_symbols


def test_operating_stock_symbols_excludes_benchmark_and_duplicates():
    result = operating_stock_symbols(
        ["AAPL", "SPY", "AAPL", "MISSING"],
        pd.Index(["AAPL", "SPY"]),
        exclude={"SPY"},
    )
    assert result == ["AAPL"]
