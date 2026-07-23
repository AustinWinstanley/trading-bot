import json

import numpy as np
import pandas as pd
import pytest

from backtest.return_uncertainty_study import (
    bootstrap_outcomes,
    circular_block_indices,
    severe_delisting_drags,
)


def test_circular_blocks_preserve_contiguous_observations():
    rng = np.random.default_rng(7)
    indices = circular_block_indices(
        10, 8, block_size=4, simulations=3, rng=rng
    )
    assert indices.shape == (3, 8)
    for row in indices:
        assert np.all((np.diff(row[:4]) % 10) == 1)
        assert np.all((np.diff(row[4:]) % 10) == 1)


def test_bootstrap_is_reproducible_and_positive_for_constant_gains():
    returns = pd.Series([0.001] * 300)
    first = bootstrap_outcomes(
        returns, years=1, simulations=20, seed=4, chunk_size=7
    )
    second = bootstrap_outcomes(
        returns, years=1, simulations=20, seed=4, chunk_size=7
    )
    assert first == second
    assert first["cagr"]["p50"] > 0
    assert first["probability_negative_cagr"] == 0
    assert first["max_drawdown"]["p05"] == 0


def test_severe_delisting_drag_uses_annual_return_gap(tmp_path):
    path = tmp_path / "study.json"
    path.write_text(json.dumps({
        "production_portfolio": {
            "full": [
                {"portfolio": "base — survivors only", "ann_return": 0.12},
                {
                    "portfolio": "base — extended: all_zero",
                    "ann_return": 0.10,
                },
                {"portfolio": "2x — survivors only", "ann_return": 0.20},
                {
                    "portfolio": "2x — extended: all_zero",
                    "ann_return": 0.16,
                },
            ]
        }
    }))
    assert severe_delisting_drags(path) == pytest.approx(
        {"base": 0.02, "2x": 0.04}
    )
