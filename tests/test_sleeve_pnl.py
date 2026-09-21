"""Tests for engine.sleeve_pnl — the Phase 4 paper-validation monitoring
tool (docs/research.md, "Live results and the 2026-09-14 stand-down";
reports/absolute_return_paper_validation_registration.json)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine.sleeve_pnl import sleeve_pnl, validation_kill_rule_status


def journal() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE snapshots(ts TEXT, equity REAL, cash REAL, positions TEXT, diag TEXT)"
    )
    conn.execute(
        "CREATE TABLE orders(ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, qty REAL, "
        "notional REAL, limit_price REAL, stop_price REAL, reason TEXT, alpaca_id TEXT, "
        "status TEXT, requested_notional REAL, reference_price REAL, filled_qty REAL, "
        "filled_avg_price REAL, filled_at TEXT)"
    )
    return conn


def snap(conn, ts, equity, positions, origin):
    conn.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?)",
        (ts, equity, 0.0, json.dumps(positions), json.dumps({"origin": origin})),
    )


def fill(conn, ts, symbol, side, sleeve, qty, price):
    conn.execute(
        "INSERT INTO orders(ts, symbol, side, sleeve, filled_qty, filled_avg_price, status) "
        "VALUES (?,?,?,?,?,?,'filled')",
        (ts, symbol, side, sleeve, qty, price),
    )


def test_no_change_no_fills_is_all_zero():
    conn = journal()
    positions = {"SPY": {"qty": 10.0, "px": 100.0}}
    snap(conn, "T1", 1000.0, positions, {"SPY": "equity_core"})
    snap(conn, "T2", 1000.0, positions, {"SPY": "equity_core"})
    result = sleeve_pnl(conn)
    assert result["cumulative_by_bucket"] == {"equity_core": 0.0, "residual": 0.0}
    assert result["portfolio_return"] == 0.0
    assert result["spy_return"] == 0.0


def test_pure_holding_pnl_attributed_to_its_sleeve():
    conn = journal()
    snap(conn, "T1", 1000.0, {"SPY": {"qty": 10.0, "px": 100.0}}, {"SPY": "equity_core"})
    snap(conn, "T2", 1050.0, {"SPY": {"qty": 10.0, "px": 105.0}}, {"SPY": "equity_core"})
    result = sleeve_pnl(conn)
    # 10 shares * (105 - 100) = 50, no fills, no residual.
    assert result["cumulative_by_bucket"]["equity_core"] == pytest.approx(50.0)
    assert result["cumulative_by_bucket"]["residual"] == pytest.approx(0.0)
    assert result["portfolio_return"] == pytest.approx(0.05)


def test_identity_reconciles_to_the_penny_with_a_fill_inside_the_interval():
    conn = journal()
    # Half-open window: a fill stamped at T1 (the run that WROTE snapshot T1)
    # belongs to the interval T1->T2, not the one ending at T1.
    snap(conn, "T1", 1000.0, {"SPY": {"qty": 10.0, "px": 100.0}}, {"SPY": "equity_core"})
    fill(conn, "T1", "SPY", "buy", "equity_core", 5.0, 101.0)
    snap(conn, "T2", 1080.0, {"SPY": {"qty": 15.0, "px": 106.0}}, {"SPY": "equity_core"})
    result = sleeve_pnl(conn)
    # holding: 10*(106-100)=60; traded lot: 5*(106-101)=25; total 85.
    assert result["cumulative_by_bucket"]["equity_core"] == pytest.approx(85.0)
    # 1080 - 1000 - (buy cashflow 5*101=505 already reflected in equity) ...
    # equity change is 80, contributions sum to 85 -> residual explains the gap.
    total = sum(result["cumulative_by_bucket"].values())
    assert total == pytest.approx(1080.0 - 1000.0)


def test_full_exit_inside_interval_uses_last_fill_price_as_terminal_mark():
    conn = journal()
    snap(conn, "T1", 1000.0, {"SPY": {"qty": 10.0, "px": 100.0}}, {"SPY": "equity_core"})
    fill(conn, "T1", "SPY", "sell", "equity_core", 10.0, 102.0)
    snap(conn, "T2", 1020.0, {}, {})  # position fully closed: no T2 mark
    result = sleeve_pnl(conn)
    # holding: 10*(102-100)=20; closing trade: dq=-10*(102-102)=0; total 20.
    assert result["cumulative_by_bucket"]["equity_core"] == pytest.approx(20.0)
    assert result["cumulative_by_bucket"]["residual"] == pytest.approx(0.0)


def test_mom_ls_style_fallback_sleeve_from_orders_when_origin_is_missing():
    """A symbol with no entry in either snapshot's diag.origin (e.g. an
    orphaned/unattributed position) falls back to its most recent
    non-exit order's sleeve, same convention as scripts/healthcheck.py's
    held_sleeve_by_symbol."""
    conn = journal()
    fill(conn, "T0", "AAA", "buy", "mom_ls", 10.0, 50.0)
    snap(conn, "T1", 1000.0, {"AAA": {"qty": 10.0, "px": 50.0}}, {})
    snap(conn, "T2", 1010.0, {"AAA": {"qty": 10.0, "px": 51.0}}, {})
    result = sleeve_pnl(conn)
    assert result["cumulative_by_bucket"] == {"mom_ls": 10.0, "residual": 0.0}


def test_unpriced_position_falls_back_to_unattributed_label():
    conn = journal()
    snap(conn, "T1", 1000.0, {"AAA": {"qty": 1.0, "px": 10.0}}, {})
    snap(conn, "T2", 1000.0, {"AAA": {"qty": 1.0, "px": 10.0}}, {})
    result = sleeve_pnl(conn)
    assert "unattributed" in result["cumulative_by_bucket"]


def test_spy_return_and_drawdown_track_the_positions_blob_mark():
    conn = journal()
    snap(conn, "T1", 1000.0, {"SPY": {"qty": 1.0, "px": 100.0}}, {"SPY": "equity_core"})
    snap(conn, "T2", 900.0, {"SPY": {"qty": 1.0, "px": 90.0}}, {"SPY": "equity_core"})
    snap(conn, "T3", 950.0, {"SPY": {"qty": 1.0, "px": 95.0}}, {"SPY": "equity_core"})
    result = sleeve_pnl(conn)
    assert result["spy_return"] == pytest.approx(-0.05)
    assert result["spy_max_drawdown"] == pytest.approx(-0.10)
    assert result["portfolio_max_drawdown"] == pytest.approx(-0.10)


def test_since_cursor_excludes_earlier_snapshots():
    conn = journal()
    snap(conn, "T0", 500.0, {"SPY": {"qty": 1.0, "px": 50.0}}, {"SPY": "equity_core"})
    snap(conn, "T1", 1000.0, {"SPY": {"qty": 1.0, "px": 100.0}}, {"SPY": "equity_core"})
    snap(conn, "T2", 1010.0, {"SPY": {"qty": 1.0, "px": 101.0}}, {"SPY": "equity_core"})
    result = sleeve_pnl(conn, since="T1")
    assert result["since"] == "T1"
    assert result["sessions"] == 2
    assert result["start_equity"] == 1000.0


def test_fewer_than_two_snapshots_returns_empty():
    conn = journal()
    snap(conn, "T1", 1000.0, {}, {})
    assert sleeve_pnl(conn) == {}
    assert sleeve_pnl(conn, since="2099-01-01") == {}


def test_sessions_count_trading_days_not_snapshots():
    # Two full runs a day journal two snapshots a day; the registration's
    # "session 20" means the 20th day.
    conn = journal()
    spy = {"SPY": {"qty": 1.0, "px": 100.0}}
    for ts in (
        "2026-09-15T09:47:01-04:00", "2026-09-15T12:35:01-04:00",
        "2026-09-16T09:47:01-04:00", "2026-09-16T12:35:01-04:00",
        "2026-09-17T09:47:01-04:00",
    ):
        snap(conn, ts, 1000.0, spy, {"SPY": "equity_core"})
    assert sleeve_pnl(conn)["sessions"] == 3


def test_kill_rules_report_insufficient_history_before_min_sessions():
    conn = journal()
    snap(conn, "T1", 1000.0, {"SPY": {"qty": 1.0, "px": 100.0}}, {"SPY": "equity_core"})
    snap(conn, "T2", 500.0, {"SPY": {"qty": 1.0, "px": 50.0}}, {"SPY": "equity_core"})
    pnl = sleeve_pnl(conn)
    status = validation_kill_rule_status(pnl, slippage_bps=100.0, drawdown_limit_pct=10.0)
    assert status["status"] == "insufficient_history"
    assert all(r["breached"] is None for r in status["rules"].values())
    # Raw numbers are still surfaced even though no verdict is drawn.
    assert status["rules"]["K1_return_vs_spy"]["excess_return_pp"] == pytest.approx(0.0)


def test_kill_rule_k1_breaches_when_trailing_spy_by_more_than_floor():
    conn = journal()
    equities = [10000.0]
    for i in range(25):
        equities.append(equities[-1] * 0.995)  # portfolio drifts down ~11.75% total
    for i, eq in enumerate(equities):
        # SPY held flat (no move) so the whole shortfall is excess vs SPY.
        snap(conn, f"T{i}", eq, {"SPY": {"qty": 1.0, "px": 100.0}}, {"SPY": "equity_core"})
    pnl = sleeve_pnl(conn)
    status = validation_kill_rule_status(pnl, slippage_bps=1.0, drawdown_limit_pct=50.0)
    assert status["sessions"] == 26
    assert status["rules"]["K1_return_vs_spy"]["breached"] is True
    assert status["status"].startswith("KILL")
    assert "K1_return_vs_spy" in status["status"]


def test_kill_rule_k2_reports_band_not_computable_under_63_observations():
    conn = journal()
    for i in range(25):
        snap(conn, f"T{i}", 10000.0 * (0.98 ** i), {"SPY": {"qty": 1.0, "px": 100.0}}, {"SPY": "equity_core"})
    pnl = sleeve_pnl(conn)
    status = validation_kill_rule_status(pnl, slippage_bps=1.0, drawdown_limit_pct=1.0)
    k2 = status["rules"]["K2_drawdown_vs_band"]
    assert "band_not_computable" in k2["band_note"]


def test_kill_rule_k3_breaches_on_high_slippage():
    conn = journal()
    for i in range(25):
        snap(conn, f"T{i}", 10000.0, {"SPY": {"qty": 1.0, "px": 100.0}}, {"SPY": "equity_core"})
    pnl = sleeve_pnl(conn)
    status = validation_kill_rule_status(pnl, slippage_bps=40.0, drawdown_limit_pct=50.0)
    assert status["rules"]["K3_slippage"]["breached"] is True
    assert "K3_slippage" in status["status"]


def test_kill_rules_ok_when_nothing_breached():
    conn = journal()
    for i in range(25):
        snap(conn, f"T{i}", 10000.0 * (1.001 ** i), {"SPY": {"qty": 1.0, "px": 100.0}}, {"SPY": "equity_core"})
    pnl = sleeve_pnl(conn)
    status = validation_kill_rule_status(pnl, slippage_bps=1.0, drawdown_limit_pct=10.0)
    assert status["status"] == "OK"


def test_empty_pnl_dict_is_insufficient_history():
    status = validation_kill_rule_status({}, slippage_bps=None, drawdown_limit_pct=10.0)
    assert status["status"] == "insufficient_history"
    assert status["sessions"] == 0
