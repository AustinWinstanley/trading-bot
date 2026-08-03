import numpy as np
import pandas as pd

from backtest.risk_overlay_study import (
    build_overlay_stream,
    diverse_select,
    stop_distance,
)


def _synthetic(n_days=400, n_sym=60, seed=0):
    """Trending panel with enough history to clear the 252+21 warmup."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    syms = [f"S{i:02d}" for i in range(n_sym)]
    drift = np.linspace(-0.0008, 0.0008, n_sym)
    steps = rng.normal(0.0, 0.02, (n_days, n_sym)) + drift
    close = pd.DataFrame(100 * np.exp(np.cumsum(steps, axis=0)), index=idx, columns=syms)
    volume = pd.DataFrame(1e7, index=idx, columns=syms)
    return close, volume


def test_stop_distance_mirrors_the_live_formula():
    # engine/risk.py: min(max(stop_loss_pct, atr_multiple * atr/px), cap)
    atr = pd.Series([0.01, 0.05, 0.20])
    got = stop_distance(atr, floor=0.08, multiple=2.0, cap=0.15)
    # 2*0.01=0.02 -> floored to 0.08; 2*0.05=0.10 -> kept; 2*0.20=0.40 -> capped
    assert list(np.round(got, 4)) == [0.08, 0.10, 0.15]


def test_control_variant_takes_the_plain_momentum_ranks():
    close, volume = _synthetic()
    res = build_overlay_stream(close, volume, stop_mode="none", block_days=0)
    assert res.stop_exits == 0
    assert res.blocked_selections == 0
    # 20 long + 20 short, held between weekly rebalances
    assert 35 <= res.diagnostics["mean_names_held"] <= 40


def test_stops_fire_and_raise_turnover():
    close, volume = _synthetic()
    control = build_overlay_stream(close, volume, stop_mode="none", block_days=0)
    stopped = build_overlay_stream(close, volume, stop_mode="flat", flat_stop=0.05, block_days=0)
    assert stopped.stop_exits > 0
    assert stopped.diagnostics["annual_turnover"] > control.diagnostics["annual_turnover"]


def test_rotation_losses_count_even_without_stops():
    """Production blocks on ANY exit at a loss, not just stop-outs."""
    close, volume = _synthetic()
    res = build_overlay_stream(close, volume, stop_mode="none", block_days=0)
    assert res.stop_exits == 0
    assert res.loss_exits > 0          # names rotated out under water


def test_block_only_bites_when_a_barred_name_still_ranks():
    close, volume = _synthetic()
    no_stop = build_overlay_stream(close, volume, stop_mode="none", block_days=5)
    with_stop = build_overlay_stream(close, volume, stop_mode="flat", flat_stop=0.05, block_days=5)
    # A rotation exit drops the name from the ranks, so nothing is barred from
    # re-entry; a stop-out leaves it ranked, and the block then binds.
    assert no_stop.blocked_selections == 0
    assert with_stop.blocked_selections > 0


def test_diverse_select_skips_correlated_names():
    idx = pd.bdate_range("2021-01-01", periods=120)
    base = np.random.default_rng(1).normal(0, 0.01, len(idx))
    rets = pd.DataFrame({
        "A": base,                                   # rank 1
        "B": base * 0.99 + 1e-6,                     # ~perfectly correlated with A
        "C": np.random.default_rng(2).normal(0, 0.01, len(idx)),   # independent
    }, index=idx)
    picked = diverse_select(
        ["A", "B", "C"], rets, idx[-1], 2,
        max_correlation=0.5, window=60, pool=10,
    )
    assert picked == ["A", "C"]        # B skipped, walked down to C


def test_diverse_select_tops_up_when_too_few_are_diverse():
    idx = pd.bdate_range("2021-01-01", periods=120)
    base = np.random.default_rng(3).normal(0, 0.01, len(idx))
    rets = pd.DataFrame({s: base * (1 + i * 1e-6) for i, s in enumerate("ABC")}, index=idx)
    picked = diverse_select(
        list("ABC"), rets, idx[-1], 3,
        max_correlation=0.1, window=60, pool=10,
    )
    # Sizing must stay constant even when nothing is diverse enough.
    assert sorted(picked) == ["A", "B", "C"]
