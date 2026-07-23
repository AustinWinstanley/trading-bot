from __future__ import annotations

import json
import sqlite3

import pytest

from engine.attribution import build_exposure_attribution, execution_summary
from engine.risk import Position


def test_shared_position_is_allocated_across_originating_sleeves():
    result = build_exposure_attribution(
        equity=10_000,
        targets={"SPY": 0.60, "SHORT": -0.15},
        sleeve_targets={
            "core": {"SPY": 0.40},
            "trend": {"SPY": 0.20},
            "mom_ls": {"SHORT": -0.15},
        },
        positions={
            "SPY": Position("SPY", 60, 90, 100),
            "SHORT": Position("SHORT", -10, 160, 150),
        },
    )
    assert result["actual"]["long"] == 0.60
    assert result["actual"]["short"] == 0.15
    assert result["actual_by_sleeve"]["core"]["long"] == 0.40
    assert result["actual_by_sleeve"]["trend"]["long"] == 0.20
    assert result["actual_by_sleeve"]["mom_ls"]["short"] == 0.15
    assert result["actual_by_sleeve"]["mom_ls"]["unrealized_pl"] == 100


def test_position_without_current_target_remains_visible():
    result = build_exposure_attribution(
        equity=10_000,
        targets={},
        sleeve_targets={},
        positions={"OLD": Position("OLD", 10, 9, 10)},
    )
    assert result["actual_by_sleeve"]["unattributed"]["long"] == 0.01


def _journal() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE orders(
        ts, symbol, side, sleeve, qty, notional, limit_price, stop_price,
        reason, alpaca_id, status, requested_notional, reference_price,
        filled_qty, filled_avg_price, filled_at);
    CREATE TABLE rejections(
        ts, symbol, reason, sleeve, side, requested_notional);
    CREATE TABLE attribution_snapshots(
        ts, equity, target_long, target_short, target_gross, actual_long,
        actual_short, actual_gross, target_by_sleeve, actual_by_sleeve,
        targets, actual_weights, largest_symbol_gaps);
    CREATE TABLE leverage_recommendations(
        ts, profile, mode, observations, target_vol, realized_vol,
        recommended_scale, recommended_leverage, ready, reason);
    """)
    return conn


def test_execution_summary_reports_fill_shrink_and_adverse_slippage():
    conn = _journal()
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-07-23", "SPY", "buy", "core", 2, 200, 101, 90, "",
            "id", "filled", 250, 100, 2, 100.5, "2026-07-23",
        ),
    )
    conn.execute(
        "INSERT INTO rejections VALUES (?,?,?,?,?,?)",
        (
            "2026-07-23", "HIGH", "short notional 75 rounds below 1 whole share",
            "mom_ls", "short", 75,
        ),
    )
    attribution = {
        "mom_ls": {
            "long": 0.15, "short": 0.09, "net": 0.06, "gross": 0.24,
            "unrealized_pl": 10,
        }
    }
    conn.execute(
        "INSERT INTO attribution_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-07-23", 10_000, 0.85, 0.15, 1.0, 0.80, 0.09, 0.89,
            "{}", json.dumps(attribution), "{}", "{}", "[]",
        ),
    )
    conn.execute(
        "INSERT INTO leverage_recommendations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-07-23", "2x", "shadow", 40, 0.12, 0.20, 0.60, 1.20, 1,
            "shadow recommendation only",
        ),
    )
    result = execution_summary(conn, "2026-07-22")
    assert result["overall"]["approval_pct"] == 80.0
    assert result["overall"]["fill_pct"] == 100.0
    assert result["overall"]["adverse_slippage_bps"] == pytest.approx(50.0)
    assert result["rejections"]["whole_share_rounding"] == 1
    assert result["rejections"]["requested_notional"] == 75
    assert result["latest_exposure"]["actual_short"] == 0.09
    assert result["latest_leverage_recommendation"][
        "recommended_leverage"
    ] == 1.20
