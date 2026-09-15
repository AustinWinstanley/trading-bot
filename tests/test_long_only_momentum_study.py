"""Unit tests for the pre-registered long-only momentum sleeve on a small
synthetic panel: rank buffer, vol scaling bounds, offset averaging, and
no look-ahead."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest import long_only_momentum_study as study


def _panel(n_days=420, n_syms=8, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    # Deterministic drifts so the momentum ranking is known: sym0 strongest.
    drifts = np.linspace(0.002, -0.002, n_syms)
    rets = rng.normal(0, 0.005, size=(n_days, n_syms)) + drifts
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx,
                         columns=[f"S{i}" for i in range(n_syms)])
    volume = pd.DataFrame(1e6, index=idx, columns=close.columns)
    return close, volume


def test_membership_uses_rank_buffer():
    close, volume = _panel()
    mom, elig = study.momentum_and_eligibility(close, volume, lookback=60, skip=5,
                                               min_price=1.0, min_dollar_volume=1.0)
    # Force a known ranking on one rebalance day: hold S7 (worst) as incumbent
    # and check the buffer keeps it only while rank <= 2N.
    path = study.membership_path(mom, elig, top_n=2, rebalance=20, offset=0,
                                 rank_buffer_mult=2, lookback=60, skip=5)
    assert path, "no rebalance days"
    # Plain top-N every rebalance when the buffer is off.
    plain = study.membership_path(mom, elig, top_n=2, rebalance=20, offset=0,
                                  rank_buffer_mult=None, lookback=60, skip=5)
    for (d1, held1), (d2, held2) in zip(path, plain):
        assert d1 == d2
        assert len(held1) == 2 and len(held2) == 2
        # Buffered holdings are always within the top 2N of that day.
        ranked = mom.loc[d1].where(elig.loc[d1]).dropna().sort_values(ascending=False)
        top4 = set(ranked.index[:4])
        assert set(held1) <= top4


def test_rank_buffer_keeps_incumbent_between_n_and_2n():
    idx = pd.bdate_range("2021-01-01", periods=5)
    syms = ["A", "B", "C", "D"]
    # Day 0 ranking: A > B > C > D ; day 1: C > D > A > B (A drops to rank 3 <= 2N=4 -> kept)
    mom = pd.DataFrame([[4, 3, 2, 1], [2, 1, 4, 3], [2, 1, 4, 3], [2, 1, 4, 3], [2, 1, 4, 3]],
                       index=idx, columns=syms, dtype=float)
    elig = pd.DataFrame(True, index=idx, columns=syms)
    path = study.membership_path(mom, elig, top_n=2, rebalance=1, offset=0,
                                 rank_buffer_mult=2, lookback=0, skip=0)
    assert path[0][1] == ["A", "B"]
    # A (rank 3) and B (rank 4) are both within 2N=4 -> both kept.
    assert path[1][1] == ["A", "B"]
    # With no buffer the plain top-2 replaces them.
    plain = study.membership_path(mom, elig, top_n=2, rebalance=1, offset=0,
                                  rank_buffer_mult=None, lookback=0, skip=0)
    assert plain[1][1] == ["C", "D"]


def test_vol_scale_is_bounded_and_never_levers():
    close, _ = _panel()
    w = pd.DataFrame(1.0 / close.shape[1], index=close.index, columns=close.columns)
    scale = study.vol_scale(w, close, vol_target=0.20, lookback=63, bounds=(0.25, 1.0))
    assert scale.max() <= 1.0 + 1e-12
    assert scale.min() >= 0.25 - 1e-12
    # Warm-up: 1.0 until enough observations.
    assert (scale.iloc[:63] == 1.0).all()
    # A very low target must clip at the floor, a very high one at 1.0.
    assert study.vol_scale(w, close, vol_target=0.0001).iloc[-1] == pytest.approx(0.25)
    assert study.vol_scale(w, close, vol_target=100.0).iloc[-1] == pytest.approx(1.0)


def test_offset_average_of_identical_paths_equals_the_path():
    close, volume = _panel()
    single = study.target_weights(close, volume, top_n=2, rebalance=20, rebalance_offsets=(0,),
                                  vol_target=None, lookback=60, skip=5,
                                  min_price=1.0, min_dollar_volume=1.0)
    averaged = study.target_weights(close, volume, top_n=2, rebalance=20, rebalance_offsets=(0, 0, 0),
                                    vol_target=None, lookback=60, skip=5,
                                    min_price=1.0, min_dollar_volume=1.0)
    pd.testing.assert_frame_equal(single, averaged)
    assert single.sum(axis=1).max() <= 1.0 + 1e-9


def test_no_look_ahead_weights_depend_only_on_past_closes():
    close, volume = _panel()
    kwargs = dict(top_n=2, rebalance=20, rebalance_offsets=(0,), vol_target=0.2,
                  lookback=60, skip=5, min_price=1.0, min_dollar_volume=1.0)
    base = study.target_weights(close, volume, **kwargs)
    shocked = close.copy()
    cut = close.index[300]
    shocked.loc[shocked.index >= cut] *= np.exp(np.linspace(0, 2, (shocked.index >= cut).sum()))[:, None]
    after = study.target_weights(shocked, volume, **kwargs)
    # Weights strictly before the shock date are unchanged.
    pd.testing.assert_frame_equal(base.loc[base.index < cut], after.loc[after.index < cut])
