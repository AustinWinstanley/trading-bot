import sqlite3

import pytest
import yaml

from engine.config import ConfigError, load_config
from engine.leverage_overlay import apply_target_scale, recommend_leverage


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
    assert result["applied_leverage"] == 2.0


def test_active_overlay_applies_ready_recommendation():
    equities = [10_000]
    for i in range(1, 20):
        equities.append(equities[-1] * (1.04 if i % 2 else 0.96))
    result = recommend_leverage(
        _journal(equities),
        current_ts="2026-01-20T15:00:00-05:00",
        current_equity=equities[-1] * 0.96,
        fixed_leverage=2.0,
        settings={
            "mode": "active",
            "target_vol": 0.12,
            "lookback_days": 10,
            "min_observations": 10,
            "min_scale": 0.25,
        },
    )
    assert result["ready"] is True
    assert result["applied_scale"] == result["recommended_scale"]
    assert result["applied_leverage"] == result["recommended_leverage"]
    assert result["reason"] == "active target scaling"


def test_active_overlay_scales_targets_and_attribution_together():
    targets = {"SPY": 1.2, "ABC": -0.2}
    diag = {
        "sleeve_targets": {
            "core": {"SPY": 1.2},
            "mom": {"ABC": -0.2},
        },
        "gross_leverage": 2.0,
    }
    scaled, updated = apply_target_scale(
        targets,
        diag,
        {"applied_scale": 0.5, "applied_leverage": 1.0},
    )
    assert scaled == {"SPY": 0.6, "ABC": -0.1}
    assert updated["sleeve_targets"] == {
        "core": {"SPY": 0.6},
        "mom": {"ABC": -0.1},
    }
    assert updated["long_weight"] == 0.6
    assert updated["short_weight"] == 0.1
    assert updated["gross_leverage"] == 1.0


def test_prior_active_scale_is_removed_from_volatility_estimate():
    equities = [10_000]
    for i in range(1, 20):
        equities.append(equities[-1] * (1.02 if i % 2 else 0.98))
    raw = _journal(equities)
    normalized = _journal(equities)
    normalized.execute(
        "CREATE TABLE leverage_recommendations("
        "ts, mode, ready, recommended_scale)"
    )
    for day in range(1, 20):
        normalized.execute(
            "INSERT INTO leverage_recommendations VALUES (?,?,?,?)",
            (f"2026-01-{day:02d}T15:00:00-05:00", "active", 1, 0.5),
        )
    settings = {
        "mode": "shadow",
        "target_vol": 0.12,
        "lookback_days": 15,
        "min_observations": 10,
        "min_scale": 0.25,
    }
    raw_result = recommend_leverage(
        raw,
        current_ts="2026-01-20T15:00:00-05:00",
        current_equity=equities[-1] * 0.98,
        fixed_leverage=2.0,
        settings=settings,
    )
    normalized_result = recommend_leverage(
        normalized,
        current_ts="2026-01-20T15:00:00-05:00",
        current_equity=equities[-1] * 0.98,
        fixed_leverage=2.0,
        settings=settings,
    )
    assert normalized_result["realized_vol"] == pytest.approx(
        2 * raw_result["realized_vol"], rel=0.05
    )


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


def test_checked_in_profiles_keep_active_trading_disabled_by_default():
    assert load_config("config.yaml").sleeves_paper[
        "volatility_overlay"
    ]["mode"] == "off"
    assert load_config("config_2x.yaml").sleeves_paper[
        "volatility_overlay"
    ]["mode"] == "shadow"


def test_config_accepts_explicit_active_overlay(tmp_path):
    raw = yaml.safe_load(open("config_2x.yaml"))
    raw["paper_portfolio"]["volatility_overlay"]["mode"] = "active"
    path = tmp_path / "active.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert load_config(path).sleeves_paper[
        "volatility_overlay"
    ]["mode"] == "active"


def test_config_rejects_unknown_overlay_mode(tmp_path):
    raw = yaml.safe_load(open("config_2x.yaml"))
    raw["paper_portfolio"]["volatility_overlay"]["mode"] = "aggressive"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="volatility_overlay.mode"):
        load_config(path)
