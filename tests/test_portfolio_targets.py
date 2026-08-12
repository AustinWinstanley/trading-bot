from types import SimpleNamespace

import pandas as pd

from engine.portfolio import tsmom_targets


def _cfg(floor: float):
    return SimpleNamespace(sleeves_paper={
        "sleeves": {"tsmom": 0.25},
        "tsmom_lookback_days": 252,
        "tsmom_universe": ["LIQUID", "THIN"],
        "tsmom_min_dollar_volume": floor,
    })


def test_tsmom_drops_illiquid_target_after_normalization_without_redistributing():
    dates = pd.date_range("2024-01-01", periods=280, freq="B")
    close = pd.Series([100 + i / 10 for i in range(len(dates))], index=dates)
    bars = {
        "LIQUID": pd.DataFrame({"close": close, "volume": 100_000.0}),
        "THIN": pd.DataFrame({"close": close, "volume": 10.0}),
    }
    unfiltered = tsmom_targets(_cfg(0), bars)
    filtered = tsmom_targets(_cfg(1_000_000), bars)

    assert set(unfiltered) == {"LIQUID", "THIN"}
    assert set(filtered) == {"LIQUID"}
    assert filtered["LIQUID"] == unfiltered["LIQUID"]
    assert sum(filtered.values()) < 0.25


def test_tsmom_liquidity_ignores_incomplete_latest_bar():
    dates = pd.date_range("2024-01-01", periods=280, freq="B")
    close = pd.Series([100 + i / 10 for i in range(len(dates))], index=dates)
    volume = pd.Series(100_000.0, index=dates)
    volume.iloc[-1] = 0.0
    bars = {
        symbol: pd.DataFrame({"close": close, "volume": volume})
        for symbol in ("LIQUID", "THIN")
    }
    assert set(tsmom_targets(_cfg(1_000_000), bars)) == {"LIQUID", "THIN"}
