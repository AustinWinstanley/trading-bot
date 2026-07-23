import sqlite3

import pytest

from engine.config import load_config
from engine.leverage_overlay import recommend_leverage


def _journal(equities):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE snapshots(ts, equity)")
    for day, equity in enumerate(equities, 1):
        conn.execute(
            "INSERT INTO snapshots VALUES (?,?)",
            (f"2026-01-{day:02d}T15:00:00-05:00", equity),
        )
    return conn


def test_overlay_stays_fixed_until_enough_daily_observations():
    result = recommend_leverage(
        _journal([10_000, 10_100, 10_000]),
        current_ts="2026-01-04T15:00:00-05:00",
        current_equity=10_050,
        fixed_leverage=2.0,
        settings={
            "mode": "shadow",
            "target_vol": 0.12,
            "lookback_days": 63,
            "min_observations": 10,
            "min_scale": 0.25,
        },
    )
    assert result["ready"] is False
    assert result["recommended_leverage"] == 2.0


def test_overlay_recommends_less_leverage_when_volatility_is_high():
    equities = [10_000]
    for i in range(1, 20):
        equities.append(equities[-1] * (1.04 if i % 2 else 0.96))
    result = recommend_leverage(
        _journal(equities),
        current_ts="2026-01-20T15:00:00-05:00",
        current_equity=equities[-1] * 0.96,
        fixed_leverage=2.0,
        settings={
            "mode": "shadow",
            "target_vol": 0.12,
            "lookback_days": 10,
            "min_observations": 10,
            "min_scale": 0.25,
        },
    )
    assert result["ready"] is True
    assert result["realized_vol"] > 0.12
    assert 0.5 <= result["recommended_leverage"] < 2.0


def test_same_day_retry_replaces_instead_of_adding_observation():
    conn = _journal([10_000, 10_100])
    result = recommend_leverage(
        conn,
        current_ts="2026-01-02T16:00:00-05:00",
        current_equity=10_200,
        fixed_leverage=2.0,
        settings={
            "mode": "shadow",
            "target_vol": 0.12,
            "lookback_days": 10,
            "min_observations": 2,
            "min_scale": 0.25,
        },
    )
    assert result["observations"] == 1


def test_off_mode_does_not_read_or_change_leverage():
    result = recommend_leverage(
        _journal([]),
        current_ts="2026-01-01T15:00:00-05:00",
        current_equity=10_000,
        fixed_leverage=1.0,
        settings={"mode": "off"},
    )
    assert result["reason"] == "overlay off"
    assert result["recommended_leverage"] == pytest.approx(1.0)


def test_checked_in_profiles_keep_overlay_off_or_shadow_only():
    assert load_config("config.yaml").sleeves_paper[
        "volatility_overlay"
    ]["mode"] == "off"
    assert load_config("config_2x.yaml").sleeves_paper[
        "volatility_overlay"
    ]["mode"] == "shadow"
