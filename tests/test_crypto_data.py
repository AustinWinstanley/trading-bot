"""Pure-logic tests for engine/crypto_data.py — synthetic payloads, no network."""

import datetime as dt

import pandas as pd
import pytest

from engine.crypto_data import (
    cache_is_fresh,
    cache_path,
    completed_daily_closes,
    fetch_daily_bars,
    get_crypto_bars,
    parse_crypto_bars,
)


def _bar(t: str, c: float, o: float = 0.0) -> dict:
    return {"t": t, "o": o or c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 10.0, "n": 5, "vw": c}


def test_parse_crypto_bars_builds_sorted_renamed_frames():
    payload = {
        "BTC/USD": [
            _bar("2021-01-02T06:00:00Z", 32000.0),
            _bar("2021-01-01T06:00:00Z", 29000.0),
            # duplicate timestamp: last row wins, matching engine.data.get_bars
            _bar("2021-01-02T06:00:00Z", 32100.0),
        ],
        "ETH/USD": [],
    }
    frames = parse_crypto_bars(payload)
    assert set(frames) == {"BTC/USD"}  # empty row lists produce no frame
    df = frames["BTC/USD"]
    assert list(df.index) == sorted(df.index)
    assert df.index.tz is not None
    assert {"open", "high", "low", "close", "volume", "trade_count", "vwap"} <= set(df.columns)
    assert df.loc[pd.Timestamp("2021-01-02T06:00:00Z"), "close"] == 32100.0
    assert len(df) == 2


def test_completed_daily_closes_drops_partial_current_utc_day():
    frames = parse_crypto_bars(
        {"BTC/USD": [_bar("2026-08-10T05:00:00Z", 100.0), _bar("2026-08-11T05:00:00Z", 105.0)]}
    )
    close = completed_daily_closes(frames["BTC/USD"], today=dt.date(2026, 8, 11))
    assert len(close) == 1
    assert close.iloc[0] == 100.0


class StubClient:
    """Mimics AlpacaClient's `_get(base, path, params)` surface only."""

    data_base = "https://data.example"

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def _get(self, base, path, params=None, **kwargs):
        self.calls.append((base, path, dict(params or {})))
        return self.pages.pop(0)


def test_get_crypto_bars_follows_next_page_token_and_merges():
    pages = [
        {
            "bars": {"BTC/USD": [_bar("2021-01-01T06:00:00Z", 29000.0)]},
            "next_page_token": "tok1",
        },
        {
            "bars": {
                "BTC/USD": [_bar("2021-01-02T06:00:00Z", 32000.0)],
                "ETH/USD": [_bar("2021-01-01T06:00:00Z", 730.0)],
            },
            "next_page_token": None,
        },
    ]
    client = StubClient(pages)
    frames = get_crypto_bars(
        client, ["BTC/USD", "ETH/USD"], dt.date(2021, 1, 1), dt.date(2021, 1, 3)
    )
    assert len(client.calls) == 2
    assert client.calls[0][1] == "/v1beta3/crypto/us/bars"
    assert "page_token" not in client.calls[0][2]
    assert client.calls[1][2]["page_token"] == "tok1"
    # crypto endpoint takes neither feed nor adjustment
    assert "feed" not in client.calls[0][2] and "adjustment" not in client.calls[0][2]
    assert len(frames["BTC/USD"]) == 2
    assert len(frames["ETH/USD"]) == 1


def test_cache_is_fresh_bounds():
    idx = pd.date_range("2021-01-01", "2021-06-30", freq="D", tz="UTC")
    cached = pd.DataFrame({"close": 1.0}, index=idx)
    assert cache_is_fresh(cached, dt.date(2021, 1, 1), dt.date(2021, 7, 2))
    assert not cache_is_fresh(cached, dt.date(2021, 1, 1), dt.date(2021, 8, 1))
    assert not cache_is_fresh(cached, dt.date(2020, 6, 1), dt.date(2021, 6, 30))
    assert not cache_is_fresh(cached.iloc[:0], dt.date(2021, 1, 1), dt.date(2021, 6, 30))


def test_fetch_daily_bars_writes_cache_then_serves_from_it(tmp_path):
    pages = [
        {
            "bars": {
                "BTC/USD": [
                    _bar("2021-01-01T06:00:00Z", 29000.0),
                    _bar("2021-01-02T06:00:00Z", 32000.0),
                ]
            },
            "next_page_token": None,
        }
    ]
    client = StubClient(pages)
    frames = fetch_daily_bars(
        ["BTC/USD"],
        dt.date(2021, 1, 1),
        dt.date(2021, 1, 2),
        cache_dir=tmp_path,
        client=client,
    )
    assert cache_path("BTC/USD", tmp_path).exists()
    assert len(frames["BTC/USD"]) == 2

    # Second call: cache is fresh, so no client is needed at all — passing
    # client=None would attempt AlpacaClient construction only on a miss.
    again = fetch_daily_bars(
        ["BTC/USD"], dt.date(2021, 1, 1), dt.date(2021, 1, 2), cache_dir=tmp_path, client=None
    )
    assert again["BTC/USD"]["close"].tolist() == frames["BTC/USD"]["close"].tolist()


def test_fetch_daily_bars_raises_on_symbols_with_no_data(tmp_path):
    client = StubClient([{"bars": {}, "next_page_token": None}])
    with pytest.raises(RuntimeError, match="NOPE/USD"):
        fetch_daily_bars(
            ["NOPE/USD"],
            dt.date(2021, 1, 1),
            dt.date(2021, 1, 2),
            cache_dir=tmp_path,
            client=client,
        )
