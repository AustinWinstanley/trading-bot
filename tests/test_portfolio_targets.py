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
