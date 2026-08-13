import sqlite3

import pytest

from engine.execution_timing import size_regression_summary, timing_summary


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


def test_size_regression_reports_a_hard_floor_below_three_fills():
    base = _journal([("2026-08-10T09:47:00-04:00", "A", "buy", "mom_ls", 1, 100, 100.5)])
    leveraged = _journal([("2026-08-10T09:51:00-04:00", "A", "buy", "mom_ls", 2, 100, 101.0)])
    result = size_regression_summary(base, leveraged, minimum_fills=50)
    assert result["decision"] == "insufficient_data"
    assert result["base_fills"] == 1
    assert result["leveraged_fills"] == 1


def test_size_regression_reports_insufficient_data_below_the_threshold():
    ts = "2026-08-10T09:47:00-04:00"
    base = _journal([(ts, f"S{i}", "buy", "mom_ls", 1, 100, 100.5) for i in range(3)])
    leveraged = _journal([(ts, f"L{i}", "buy", "mom_ls", 2, 100, 101.0) for i in range(3)])
    result = size_regression_summary(base, leveraged, minimum_fills=50)
    assert "insufficient data" in result["recommendation"]
    assert result["total_fills"] == 6


def test_size_regression_attributes_a_pure_size_effect_to_size(monkeypatch):
    """Construct fills where slippage is an exact linear function of
    notional, identical in both accounts (no genuine account/schedule
    effect at all) — the regression should attribute ~100% of any raw gap
    to size, not to being the 2x account."""
    rows = []
    ts = "2026-08-10T09:47:00-04:00"
    # slippage_bps = 0.1 * notional, same relationship in both accounts;
    # the 2x account just happens to trade larger notional on average.
    for i, notional in enumerate([50, 60, 70, 80, 90]):
        fill_price = 100 * (1 + 0.1 * notional / 10_000)
        rows.append((ts, f"S{i}", "buy", "mom_ls", notional / 100, 100, fill_price))
    base = _journal(rows)
    lev_rows = []
    for i, notional in enumerate([100, 120, 140, 160, 180]):
        fill_price = 100 * (1 + 0.1 * notional / 10_000)
        lev_rows.append((ts, f"L{i}", "buy", "mom_ls", notional / 100, 100, fill_price))
    leveraged = _journal(lev_rows)
    result = size_regression_summary(base, leveraged, minimum_fills=5)
    assert result["size_explained_fraction_of_raw_gap"] == pytest.approx(1.0, abs=0.05)
    assert result["notional_adjusted_leveraged_minus_base_bps"] == pytest.approx(0.0, abs=0.5)
    assert "confounded by size" in result["recommendation"]


def test_size_regression_reports_a_residual_when_size_does_not_fully_explain_it():
    """A large is_2x-only jump that notional cannot explain should leave a
    non-trivial residual and recommend continuing, not canceling."""
    ts = "2026-08-10T09:47:00-04:00"
    base = _journal([
        (ts, f"S{i}", "buy", "mom_ls", 0.75, 100, 100.001)
        for i in range(10)
    ])
    leveraged = _journal([
        (ts, f"L{i}", "buy", "mom_ls", 1.49, 100, 100.5)  # much worse fill, same-ish size
        for i in range(10)
    ])
    result = size_regression_summary(base, leveraged, minimum_fills=5)
    assert result["size_explained_fraction_of_raw_gap"] < 0.75
    assert "residual difference persists" in result["recommendation"]
