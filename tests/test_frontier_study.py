import datetime as dt

from backtest.frontier_study import completed_crypto_prices


def test_completed_crypto_prices_excludes_current_incomplete_day():
    prices = completed_crypto_prices(
        [
            {"t": "2026-07-22T00:00:00Z", "c": 100},
            {"t": "2026-07-23T00:00:00Z", "c": 105},
        ],
        today=dt.date(2026, 7, 23),
    )
    assert len(prices) == 1
    assert prices.iloc[0] == 100
