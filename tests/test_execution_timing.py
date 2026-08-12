import sqlite3

import pytest

from engine.execution_timing import timing_summary


def _journal(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE orders(
            ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, filled_qty REAL,
            reference_price REAL, filled_avg_price REAL)
    """)
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", rows)
    return conn


def test_timing_summary_matches_nearest_same_session_fill_once():
    base = _journal([
        ("2026-08-10T09:47:00-04:00", "A", "buy", "mom_ls", 2, 100, 100.01),
        ("2026-08-10T13:35:00-04:00", "A", "buy", "mom_ls", 2, 101, 101.00),
        ("2026-08-10T09:47:00-04:00", "B", "sell", "trend", 1, 50, 49),
    ])
    leveraged = _journal([
        ("2026-08-10T09:51:00-04:00", "A", "buy", "mom_ls", 4, 100, 100.08),
        ("2026-08-10T13:39:00-04:00", "A", "buy", "mom_ls", 4, 101, 101.02),
    ])
    result = timing_summary(base, leveraged, control_min_pairs=2)
    assert result["matched_fills"] == 2
    assert result["matched_sessions"] == 1
    assert result["control_complete"] is True
    assert result["average_schedule_delta_minutes"] == 4
    assert result["leveraged_minus_base_bps"] > 0


def test_adverse_slippage_sign_is_reversed_for_sells():
    base = _journal([
        ("2026-08-10T09:47:00-04:00", "A", "sell", "mom_ls", 1, 100, 99.99),
    ])
    leveraged = _journal([
        ("2026-08-10T09:51:00-04:00", "A", "sell", "mom_ls", 1, 100, 99.90),
    ])
    result = timing_summary(base, leveraged)
    assert result["base_adverse_slippage_bps"] == pytest.approx(1)
    assert result["leveraged_adverse_slippage_bps"] == pytest.approx(10)


def test_fill_outside_matching_window_is_excluded():
    base = _journal([
        ("2026-08-10T09:47:00-04:00", "A", "buy", "mom_ls", 1, 100, 100),
    ])
    leveraged = _journal([
        ("2026-08-10T10:47:00-04:00", "A", "buy", "mom_ls", 1, 100, 100),
    ])
    assert timing_summary(base, leveraged)["matched_fills"] == 0
