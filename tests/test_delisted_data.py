import pandas as pd

from backtest.delisted_data import candidate_symbols


def metadata() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["GOOD", "NASDAQ", "Stock", "USD", "2010-01-01", "2023-01-01"],
            ["LIVE", "NYSE", "Stock", "USD", "2010-01-01", "2023-01-01"],
            ["DUP", "NYSE", "Stock", "USD", "2010-01-01", "2022-01-01"],
            ["DUP", "NASDAQ", "Stock", "USD", "2023-01-01", "2025-01-01"],
            ["OPTWW", "NASDAQ", "Stock", "USD", "2020-01-01", "2024-01-01"],
            ["SPACU", "NASDAQ", "Stock", "USD", "2020-01-01", "2024-01-01"],
            ["ABCDR", "NASDAQ", "Stock", "USD", "2020-01-01", "2024-01-01"],
            ["ETF", "NYSE", "ETF", "USD", "2010-01-01", "2023-01-01"],
            ["PINK", "PINK", "Stock", "USD", "2010-01-01", "2023-01-01"],
            ["SHORT", "NYSE", "Stock", "USD", "2022-12-01", "2023-01-01"],
        ],
        columns=[
            "ticker",
            "exchange",
            "assetType",
            "priceCurrency",
            "startDate",
            "endDate",
        ],
    )


def test_candidate_filter_excludes_active_recycled_and_derivative_symbols():
    result = candidate_symbols(metadata(), active_symbols={"LIVE"})
    assert result["ticker"].tolist() == ["GOOD"]
