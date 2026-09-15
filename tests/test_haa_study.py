"""Unit tests for the pre-registered HAA sleeve on a synthetic close frame."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest import haa_study as study


def _close(n_days=320, seed=3, canary_up=True):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-04", periods=n_days)
    frame = {}
    for i, s in enumerate(study.INSTRUMENTS):
        drift = 0.001 - 0.0002 * i
        frame[s] = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, n_days) + drift))
    close = pd.DataFrame(frame, index=idx)
    close[study.CANARY] = 100 * np.exp(np.cumsum(np.full(n_days, 0.0005 if canary_up else -0.0005)))
    return close


def test_canary_off_goes_fully_defensive():
    close = _close(canary_up=False)
    w = study.target_weights(close)
    live = w.loc[w.sum(axis=1) > 0]
    assert len(live)
    assert (live[study.OFFENSIVE].drop(columns=["IEF"]).sum(axis=1) == 0).all()
    assert (live[study.DEFENSIVE].sum(axis=1) == 1.0).all()


def test_canary_on_holds_top_four_equal_weight_with_negative_redirected():
    close = _close(canary_up=True)
    score = study.momentum_score(close)
    day = study.month_end_days(close.index)[-2]
    s = score.loc[day].copy()
    s[study.CANARY] = 1.0
    s[study.OFFENSIVE] = [0.5, 0.4, 0.3, -0.1, -0.2, -0.3, -0.4, -0.5]   # 4th best is negative
    s["BIL"], s["IEF"] = 0.01, -0.4
    alloc = study.allocation_for(s, top_k=4)
    assert alloc["SPY"] == alloc["IWM"] == alloc["EFA"] == 0.25
    assert alloc["BIL"] == 0.25          # the negative fourth slot went to the defensive pick
    assert abs(sum(alloc.values()) - 1.0) < 1e-12


def test_weights_change_only_after_month_ends_and_sum_to_at_most_one():
    close = _close()
    w = study.target_weights(close)
    assert w.sum(axis=1).max() <= 1.0 + 1e-12
    changes = w.index[(w.diff().abs().sum(axis=1) > 0)]
    month_ends = set(study.month_end_days(close.index))
    for d in changes:
        prev = close.index[close.index.get_loc(d) - 1]
        assert prev in month_ends, f"weights changed on {d} whose previous day is not a month end"


def test_no_look_ahead():
    close = _close()
    base = study.target_weights(close)
    cut = close.index[200]
    shocked = close.copy()
    shocked.loc[shocked.index >= cut, study.OFFENSIVE] *= 3.0
    after = study.target_weights(shocked)
    # A shock from `cut` onward cannot move weights decided at or before the
    # month end preceding `cut`; weights strictly before `cut` are identical.
    pd.testing.assert_frame_equal(base.loc[base.index < cut], after.loc[after.index < cut])
