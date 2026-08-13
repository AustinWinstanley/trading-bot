from types import SimpleNamespace

import pandas as pd

from engine.portfolio import trend_targets, tsmom_targets


def _cfg(floor: float):
    return SimpleNamespace(sleeves_paper={
        "sleeves": {"tsmom": 0.25},
        "tsmom_lookback_days": 252,
        "tsmom_universe": ["LIQUID", "THIN"],
        "tsmom_min_dollar_volume": floor,
    })


def _trend_cfg(reserve_symbol=None):
    return SimpleNamespace(sleeves_paper={
        "sleeves": {"trend": 0.20},
        "trend_symbol": "SPY",
        "trend_ma_days": 200,
        **({"trend_reserve_symbol": reserve_symbol} if reserve_symbol else {}),
    })


def _trend_bars(*, above_ma: bool, reserve_symbol: str | None = None, reserve_bars: bool = True):
    dates = pd.date_range("2024-01-01", periods=260, freq="B")
    if above_ma:
        close = pd.Series([100 + i / 20 for i in range(len(dates))], index=dates)
    else:
        # Ramps up then drops sharply so the last completed close sits below
        # its 200-day average.
        ramp = pd.Series([100 + i / 5 for i in range(200)])
        drop = pd.Series([50.0] * 60)
        close = pd.Series(pd.concat([ramp, drop], ignore_index=True).values, index=dates)
    bars = {"SPY": pd.DataFrame({"close": close, "volume": 1_000_000.0})}
    if reserve_symbol and reserve_bars:
        bars[reserve_symbol] = pd.DataFrame({"close": [100.0] * len(dates), "volume": 1_000_000.0}, index=dates)
    return bars


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
    # Close ramps up through bar -260 (so trailing momentum is positive and
    # the signal fires), then goes flat for the last 21 bars, where the
    # liquidity window actually operates — decoupling momentum from the
    # dollar-volume calculation. Real-bar volume is picked so the 20
    # COMPLETED bars average just above the floor ($1.035M) while including
    # today's zeroed partial bar in a 20-bar window would average just below
    # it ($0.983M) — the two behaviors give different pass/fail verdicts, so
    # this test actually discriminates between them (the prior version used
    # a volume level both behaviors cleared, so it passed either way).
    dates = pd.date_range("2024-01-01", periods=280, freq="B")
    ramp = pd.Series([100 + i / 5 for i in range(259)])
    flat = pd.Series([150.0] * 21)
    close = pd.concat([ramp, flat], ignore_index=True)
    close.index = dates
    volume = pd.Series(6_900.0, index=dates)
    volume.iloc[-1] = 0.0
    bars = {"NEAR_FLOOR": pd.DataFrame({"close": close, "volume": volume})}
    cfg = _cfg(1_000_000)
    cfg.sleeves_paper["tsmom_universe"] = ["NEAR_FLOOR"]
    assert set(tsmom_targets(cfg, bars)) == {"NEAR_FLOOR"}


def test_tsmom_keeps_held_illiquid_target_instead_of_forcing_a_sell():
    # A held symbol below the floor must keep its target — dropping it would
    # make the daily diff read "target 0" against a real position, and
    # sells/covers pass the risk gate unconditionally, so that would be an
    # unconditional forced exit driven only by a liquidity screen, not a
    # signal decision. See the docstring in engine/portfolio.py.
    dates = pd.date_range("2024-01-01", periods=280, freq="B")
    close = pd.Series([100 + i / 10 for i in range(len(dates))], index=dates)
    bars = {
        "LIQUID": pd.DataFrame({"close": close, "volume": 100_000.0}),
        "THIN": pd.DataFrame({"close": close, "volume": 10.0}),
    }
    cfg = _cfg(1_000_000)

    not_held = tsmom_targets(cfg, bars, held=frozenset())
    held = tsmom_targets(cfg, bars, held=frozenset({"THIN"}))

    assert set(not_held) == {"LIQUID"}
    assert set(held) == {"LIQUID", "THIN"}
    assert held["THIN"] > 0


def test_trend_targets_invests_in_symbol_when_above_ma():
    bars = _trend_bars(above_ma=True)
    targets = trend_targets(_trend_cfg(), bars)
    assert targets == {"SPY": 0.20}


def test_trend_targets_idle_cash_when_below_ma_and_no_reserve_configured():
    bars = _trend_bars(above_ma=False)
    targets = trend_targets(_trend_cfg(), bars)
    assert targets == {}


def test_trend_targets_invests_reserve_when_below_ma_and_reserve_configured():
    # reports/cash_reserve_study.json: idle 20% reserve invested in a
    # near-zero-duration instrument instead of sitting at 0% — 2x-lab only.
    bars = _trend_bars(above_ma=False, reserve_symbol="BIL")
    targets = trend_targets(_trend_cfg(reserve_symbol="BIL"), bars)
    assert targets == {"BIL": 0.20}


def test_trend_targets_falls_back_to_idle_cash_if_reserve_bars_missing():
    bars = _trend_bars(above_ma=False, reserve_symbol="BIL", reserve_bars=False)
    targets = trend_targets(_trend_cfg(reserve_symbol="BIL"), bars)
    assert targets == {}


def test_trend_targets_reserve_never_applies_when_trend_is_on():
    bars = _trend_bars(above_ma=True, reserve_symbol="BIL")
    targets = trend_targets(_trend_cfg(reserve_symbol="BIL"), bars)
    assert targets == {"SPY": 0.20}
