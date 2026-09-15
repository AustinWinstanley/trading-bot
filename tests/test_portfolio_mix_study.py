"""Unit tests for the pre-registered mix study's composition helpers."""

from __future__ import annotations

import pandas as pd

from backtest import portfolio_mix_study as study


def test_registered_mixes_match_the_registration_document():
    import json
    reg = json.loads(open("reports/absolute_return_campaign_registration.json").read())
    assert study.MIXES == {k: v for k, v in reg["mixes"]["list"].items()}
    for weights in study.MIXES.values():
        assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_mix_weights_scale_and_sum_sleeve_frames():
    idx = pd.bdate_range("2024-01-01", periods=4)
    frames = {
        "equity_core": pd.DataFrame({"SPY": 1.0}, index=idx),
        "lev_trend:QQQ/QLD": pd.DataFrame({"QLD": [1.0, 1.0, 0.0, 0.0], "BIL": [0.0, 0.0, 1.0, 1.0]}, index=idx),
        "haa": pd.DataFrame({"SPY": 0.25, "TLT": 0.25, "BIL": 0.5}, index=idx),
    }
    mix = {"equity_core": 0.55, "lev_trend": 0.20, "haa": 0.25}
    w = study.mix_weights(mix, frames, "QQQ/QLD", idx)
    assert w.loc[idx[0], "SPY"] == 0.55 + 0.25 * 0.25       # shared symbol sums across sleeves
    assert w.loc[idx[0], "QLD"] == 0.20
    assert w.loc[idx[3], "BIL"] == 0.20 + 0.25 * 0.5
    assert (w.sum(axis=1) - 1.0).abs().max() < 1e-12


def test_cell_windows_exclude_stress_for_stock_mixes_and_clip_early():
    first = pd.Timestamp("2021-08-26")
    cells = study.cell_windows(first, pd.Timestamp("2026-08-12"), stress_ok=False)
    assert cells["early_2020_2022"] == ("2021-08-26", "2022-12-30")
    assert cells["heldout_2023_plus"] == ("2023-01-03", "2026-08-12")
    assert all(v is None for k, v in cells.items() if k.startswith("stress:"))
    cells = study.cell_windows(pd.Timestamp("2006-06-21"), pd.Timestamp("2026-08-12"), stress_ok=True)
    assert cells["stress:GFC"] is not None


def test_cost_map_charges_etfs_and_stocks_differently():
    m = study.cost_map(["SPY", "AAPL"], {"SPY"})
    assert m == {"SPY": study.ETF_COST_BPS, "AAPL": study.STOCK_COST_BPS}
