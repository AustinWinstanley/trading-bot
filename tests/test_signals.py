"""Tests for the strategy sleeves.

The single most valuable test here is `test_no_lookahead_*`: signals computed
as of date D must be byte-identical whether or not the frame also contains bars
after D. A lookahead bug produces a beautiful backtest and a losing live
account, and it is invisible unless you test for it explicitly.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from engine.config import load_config
from engine.data import atr, avg_dollar_volume, momentum_skip, rsi, sma
from engine.signals import (
    build_signals,
    leveraged_signals,
    market_regime,
    mean_reversion_signals,
    momentum_signals,
    passes_universe_filters,
    pead_candidates,
    realised_vol,
)


@pytest.fixture
def cfg():
    return load_config()


def make_bars(closes, *, start="2024-01-01", volume=1_000_000, highs=None, lows=None, opens=None):
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="B", tz="UTC")
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": opens if opens is not None else closes,
            "high": highs if highs is not None else closes * 1.01,
            "low": lows if lows is not None else closes * 0.99,
            "close": closes,
            "volume": np.full(n, volume, dtype=float),
        },
        index=idx,
    )


def trending_up(n=400, start_price=100.0, daily=0.001):
    return make_bars(start_price * (1 + daily) ** np.arange(n), volume=5_000_000)


def trending_down(n=400, start_price=100.0, daily=-0.001):
    return make_bars(start_price * (1 + daily) ** np.arange(n), volume=5_000_000)


# --------------------------------------------------------------------------
# Lookahead — the test that matters most
# --------------------------------------------------------------------------


def test_no_lookahead_in_momentum(cfg):
    """Appending future bars must not change a signal computed as of today."""
    universe = cfg.sleeves["momentum"]["universe"]
    rng = np.random.default_rng(7)

    full = {}
    for i, sym in enumerate(universe + ["SPY"]):
        drift = 0.0015 - 0.0002 * i
        path = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, 600))
        full[sym] = make_bars(path, volume=10_000_000)

    as_of = full["SPY"].index[400].date()
    truncated = {s: d.iloc[:401] for s, d in full.items()}

    a = momentum_signals(full, as_of, cfg)
    b = momentum_signals(truncated, as_of, cfg)

    assert [(s.symbol, s.action, round(s.strength, 10)) for s in a] == [
        (s.symbol, s.action, round(s.strength, 10)) for s in b
    ]


def test_no_lookahead_in_full_signal_build(cfg):
    universe = cfg.sleeves["momentum"]["universe"] + cfg.sleeves["leveraged"]["symbols"] + ["SPY"]
    rng = np.random.default_rng(11)
    full = {
        sym: make_bars(100 * np.cumprod(1 + 0.0008 + rng.normal(0, 0.012, 600)), volume=10_000_000)
        for sym in universe
    }
    as_of = full["SPY"].index[450].date()
    truncated = {s: d.iloc[:451] for s, d in full.items()}

    assert build_signals(full, as_of, cfg) == build_signals(truncated, as_of, cfg)


def test_slice_excludes_bars_after_as_of(cfg):
    df = trending_up(300)
    as_of = df.index[200].date()
    regime_full = market_regime({"SPY": df}, as_of)
    regime_cut = market_regime({"SPY": df.iloc[:201]}, as_of)
    assert regime_full == regime_cut


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------


def test_sma_matches_manual_mean():
    df = make_bars(list(range(1, 51)))
    assert sma(df["close"], 10).iloc[-1] == pytest.approx(np.mean(range(41, 51)))


def test_rsi_is_100_when_every_day_is_an_up_day():
    df = make_bars(np.arange(1, 30, dtype=float))
    assert rsi(df["close"], 2).iloc[-1] == pytest.approx(100.0)


def test_rsi_is_zero_when_every_day_is_a_down_day():
    df = make_bars(np.arange(30, 1, -1, dtype=float))
    assert rsi(df["close"], 2).iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_stays_within_bounds_on_random_data():
    rng = np.random.default_rng(3)
    df = make_bars(100 * np.cumprod(1 + rng.normal(0, 0.02, 500)))
    r = rsi(df["close"], 2).dropna()
    assert r.min() >= 0 and r.max() <= 100


def test_atr_is_positive_and_tracks_range():
    df = make_bars([100] * 30, highs=np.full(30, 105.0), lows=np.full(30, 95.0))
    assert atr(df, 14).iloc[-1] == pytest.approx(10.0, rel=0.01)


def test_momentum_skip_ignores_the_most_recent_window():
    # Flat for 200 days, then a spike in the last 5. Skipping 21 days must
    # exclude the spike entirely.
    closes = np.concatenate([np.full(200, 100.0), np.full(5, 200.0)])
    s = pd.Series(closes)
    assert momentum_skip(s, 126, 21).iloc[-1] == pytest.approx(0.0)


def test_avg_dollar_volume():
    df = make_bars([10.0] * 30, volume=1_000)
    assert avg_dollar_volume(df, 20).iloc[-1] == pytest.approx(10_000)


def test_realised_vol_is_higher_for_a_noisier_series():
    rng = np.random.default_rng(5)
    calm = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.002, 100)))
    wild = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.03, 100)))
    assert realised_vol(wild) > realised_vol(calm)


# --------------------------------------------------------------------------
# Regime
# --------------------------------------------------------------------------


def test_regime_is_risk_on_above_the_200dma(cfg):
    r = market_regime({"SPY": trending_up(400)}, dt.date(2025, 6, 2))
    assert r["risk_on"]


def test_regime_is_risk_off_below_the_200dma(cfg):
    r = market_regime({"SPY": trending_down(400)}, dt.date(2025, 6, 2))
    assert not r["risk_on"]


def test_regime_is_risk_off_when_history_is_too_short(cfg):
    r = market_regime({"SPY": trending_up(50)}, dt.date(2024, 3, 1))
    assert not r["risk_on"]
    assert "insufficient" in r["reason"]


def test_momentum_exits_everything_and_rotates_to_cash_when_risk_off(cfg):
    bars = {"SPY": trending_down(400)}
    for s in cfg.sleeves["momentum"]["universe"]:
        bars[s] = trending_down(400)
    sigs = momentum_signals(bars, dt.date(2025, 6, 2), cfg, held={"XLK", "QQQ"})
    exits = {s.symbol for s in sigs if s.action == "exit"}
    assert {"XLK", "QQQ"} <= exits
    assert any(s.symbol == cfg.sleeves["momentum"]["risk_off_symbol"] and s.action == "enter" for s in sigs)


# --------------------------------------------------------------------------
# Universe filters
# --------------------------------------------------------------------------


def test_penny_stock_fails_universe_filter(cfg):
    ok, why = passes_universe_filters(make_bars([2.0] * 300, volume=100_000_000), cfg)
    assert not ok and "price" in why


def test_illiquid_name_fails_universe_filter(cfg):
    ok, why = passes_universe_filters(make_bars([50.0] * 300, volume=100), cfg)
    assert not ok and "dollar volume" in why


def test_short_history_fails_ipo_filter(cfg):
    ok, why = passes_universe_filters(make_bars([50.0] * 30, volume=10_000_000), cfg)
    assert not ok and "IPO" in why


# --------------------------------------------------------------------------
# Leveraged sleeve — the one that can do the most damage
# --------------------------------------------------------------------------


def test_leveraged_exit_is_unconditional_below_the_50dma(cfg):
    """Below the 50DMA, the sleeve exits regardless of any other gate."""
    closes = np.concatenate([100 * np.cumprod(np.full(250, 1.002)), np.full(30, 100.0)])
    bars = {"QQQ": make_bars(closes, volume=10_000_000)}
    for s in cfg.sleeves["leveraged"]["symbols"]:
        bars[s] = trending_up(280)
    sigs = leveraged_signals(bars, bars["QQQ"].index[-1].date(), cfg, held={"TQQQ"})
    assert [s.action for s in sigs] == ["exit"]
    assert "UNCONDITIONAL" in sigs[0].reason


def test_leveraged_does_not_enter_when_volatility_gate_fails(cfg):
    rng = np.random.default_rng(2)
    # Strong uptrend but very noisy -> trend gate passes, calm gate fails.
    closes = 100 * np.cumprod(1 + 0.004 + rng.normal(0, 0.045, 400))
    bars = {"QQQ": make_bars(closes, volume=10_000_000)}
    for s in cfg.sleeves["leveraged"]["symbols"]:
        bars[s] = trending_up(400)
    sigs = leveraged_signals(bars, bars["QQQ"].index[-1].date(), cfg, held=set())
    assert not [s for s in sigs if s.action == "enter"]


def test_leveraged_enters_only_in_a_calm_confirmed_uptrend(cfg):
    bars = {"QQQ": trending_up(400, daily=0.0012)}
    for s in cfg.sleeves["leveraged"]["symbols"]:
        bars[s] = trending_up(400, daily=0.003)
    sigs = leveraged_signals(bars, bars["QQQ"].index[-1].date(), cfg, held=set())
    entered = {s.symbol for s in sigs if s.action == "enter"}
    assert entered == set(cfg.sleeves["leveraged"]["symbols"])


# --------------------------------------------------------------------------
# Mean reversion
# --------------------------------------------------------------------------


def test_mean_reversion_does_not_enter_when_risk_off(cfg):
    bars = {"SPY": trending_down(400), "XLK": trending_down(400)}
    sigs = mean_reversion_signals(bars, bars["SPY"].index[-1].date(), cfg, ["XLK"], held={})
    assert not [s for s in sigs if s.action == "enter"]


def test_mean_reversion_exits_fire_even_when_risk_off(cfg):
    """Exits must never be gated on the regime — that would trap positions."""
    bars = {"SPY": trending_down(400), "XLK": trending_down(400)}
    sigs = mean_reversion_signals(
        bars, bars["SPY"].index[-1].date(), cfg, ["XLK"], held={"XLK": 99}
    )
    assert any(s.action == "exit" and s.symbol == "XLK" for s in sigs)


def test_mean_reversion_time_stop_fires(cfg):
    bars = {"SPY": trending_up(400), "XLK": trending_up(400)}
    max_days = int(cfg.sleeves["mean_reversion"]["max_hold_days"])
    sigs = mean_reversion_signals(
        bars, bars["SPY"].index[-1].date(), cfg, ["XLK"], held={"XLK": max_days}
    )
    assert any(s.action == "exit" for s in sigs)


# --------------------------------------------------------------------------
# PEAD — mechanical half only; must always defer to the LLM
# --------------------------------------------------------------------------


def _gap_up_bars(gap=0.06, vol_mult=3.0, close_pos=0.9, n=260):
    closes = list(np.full(n - 1, 100.0))
    df = make_bars(closes + [100.0], volume=1_000_000)
    prev_close = 100.0
    open_px = prev_close * (1 + gap)
    low, high = open_px * 0.99, open_px * 1.03
    close_px = low + (high - low) * close_pos
    df.iloc[-1, df.columns.get_loc("open")] = open_px
    df.iloc[-1, df.columns.get_loc("high")] = high
    df.iloc[-1, df.columns.get_loc("low")] = low
    df.iloc[-1, df.columns.get_loc("close")] = close_px
    df.iloc[-1, df.columns.get_loc("volume")] = 1_000_000 * vol_mult
    return df


def test_pead_flags_a_clean_gap_up(cfg):
    bars = {"AAA": _gap_up_bars()}
    sigs = pead_candidates(bars, bars["AAA"].index[-1].date(), cfg, ["AAA"])
    assert len(sigs) == 1


def test_pead_never_returns_a_tradeable_signal_without_llm_confirmation(cfg):
    bars = {"AAA": _gap_up_bars()}
    sigs = pead_candidates(bars, bars["AAA"].index[-1].date(), cfg, ["AAA"])
    assert all(s.needs_llm_confirmation for s in sigs)
    assert "guidance" in sigs[0].context["confirmation_required"].lower()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"gap": 0.01},        # gap too small
        {"vol_mult": 1.1},    # volume not confirming
        {"close_pos": 0.2},   # closed weak — faded the gap
    ],
)
def test_pead_rejects_incomplete_setups(cfg, kwargs):
    bars = {"AAA": _gap_up_bars(**kwargs)}
    assert pead_candidates(bars, bars["AAA"].index[-1].date(), cfg, ["AAA"]) == []


def test_pead_respects_max_positions(cfg):
    bars = {f"S{i}": _gap_up_bars(gap=0.05 + i * 0.01) for i in range(6)}
    sigs = pead_candidates(bars, bars["S0"].index[-1].date(), cfg, list(bars))
    assert len(sigs) <= int(cfg.sleeves["pead"]["max_positions"])
