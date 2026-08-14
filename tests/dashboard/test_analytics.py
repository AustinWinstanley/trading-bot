"""Tests for the Phase-2 analytics layer: equity-curve derivations,
per-day execution trends, FIFO round-trip matching, exposure history.
"""

from __future__ import annotations

import sqlite3

import pytest

from dashboard import db as dashboard_db


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "t.db"
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE snapshots(ts TEXT, equity REAL, cash REAL, positions TEXT, diag TEXT);
        CREATE TABLE orders(
            ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, qty REAL, notional REAL,
            limit_price REAL, stop_price REAL, reason TEXT, alpaca_id TEXT, status TEXT,
            requested_notional REAL, reference_price REAL, filled_qty REAL,
            filled_avg_price REAL, filled_at TEXT);
        CREATE TABLE rejections(
            ts TEXT, symbol TEXT, reason TEXT, sleeve TEXT, side TEXT,
            requested_notional REAL);
        CREATE TABLE attribution_snapshots(
            ts TEXT, equity REAL,
            target_long REAL, target_short REAL, target_gross REAL,
            actual_long REAL, actual_short REAL, actual_gross REAL,
            target_by_sleeve TEXT, actual_by_sleeve TEXT,
            targets TEXT, actual_weights TEXT, largest_symbol_gaps TEXT);
    """)
    yield c
    c.close()


class TestEquityCurveDerivations:
    def _seed(self, conn, series):
        conn.executemany(
            "INSERT INTO snapshots VALUES (?,?,?,?,?)",
            [(f"{date}T12:35:00-04:00", equity, 1000.0, "{}", "{}")
             for date, equity in series],
        )

    def test_pnl_and_return_and_drawdown(self, conn):
        self._seed(conn, [
            ("2026-08-01", 10000.0),
            ("2026-08-02", 10200.0),
            ("2026-08-03", 9900.0),
            ("2026-08-04", 10100.0),
        ])
        points = dashboard_db.equity_curve(conn, days=90)
        assert points[0]["pnl"] is None
        assert points[1]["pnl"] == 200.0
        assert points[2]["pnl"] == -300.0
        # drawdown vs running peak (10200 after day 2)
        assert points[1]["drawdown_pct"] == 0.0
        assert points[2]["drawdown_pct"] == pytest.approx(-2.941, abs=0.001)
        # return_pct is window-relative to the first point
        assert points[0]["return_pct"] == 0.0
        assert points[3]["return_pct"] == pytest.approx(1.0, abs=0.001)

    def test_drawdown_uses_all_history_peak_not_window_peak(self, conn):
        """A window starting mid-drawdown must not report 0% at its first
        point — the peak comes from the full series."""
        self._seed(conn, [
            ("2026-08-01", 12000.0),   # all-time peak, outside the window
            ("2026-08-02", 10000.0),
            ("2026-08-03", 10500.0),
        ])
        points = dashboard_db.equity_curve(conn, days=2)
        assert len(points) == 2
        assert points[0]["drawdown_pct"] == pytest.approx(-16.667, abs=0.001)

    def test_pnl_first_window_point_knows_prior_day(self, conn):
        self._seed(conn, [
            ("2026-08-01", 10000.0),
            ("2026-08-02", 10200.0),
            ("2026-08-03", 10300.0),
        ])
        points = dashboard_db.equity_curve(conn, days=2)
        assert points[0]["pnl"] == 200.0  # vs the out-of-window 08-01 close


class TestExecutionTrends:
    def test_per_day_buckets(self, conn):
        conn.executemany(
            "INSERT INTO orders(ts, symbol, side, sleeve, qty, notional, status, "
            "requested_notional, reference_price, filled_qty, filled_avg_price, filled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-08-01T09:47:00-04:00", "AAA", "buy", "s", 1, 100.0, "filled",
                 100.0, 100.0, 1.0, 100.1, "2026-08-01T09:47:10-04:00"),
                ("2026-08-01T09:47:01-04:00", "BBB", "buy", "s", 1, 100.0, "new",
                 100.0, 100.0, 0.0, None, None),
                ("2026-08-02T09:47:00-04:00", "CCC", "sell", "s", 1, 200.0, "filled",
                 200.0, 100.0, 1.0, 99.9, "2026-08-02T09:47:20-04:00"),
            ],
        )
        conn.execute(
            "INSERT INTO rejections VALUES (?,?,?,?,?,?)",
            ("2026-08-02T09:47:00-04:00", "DDD", "too small", "s", "buy", 75.0),
        )
        days = dashboard_db.execution_trends(conn, "2026-07-31")
        assert [d["date"] for d in days] == ["2026-08-01", "2026-08-02"]
        d1, d2 = days
        assert d1["orders"] == 2 and d1["filled_orders"] == 1 and d1["fill_pct"] == 50.0
        # buy filled 0.1% above ref = +10 bps adverse
        assert d1["adverse_slippage_bps"] == pytest.approx(10.0, abs=0.1)
        assert d1["avg_latency_s"] == 10.0
        # sell filled 0.1% below ref = +10 bps adverse (sign flips for sells)
        assert d2["adverse_slippage_bps"] == pytest.approx(10.0, abs=0.1)
        assert d2["rejections"] == 1 and d2["blocked_notional"] == 75.0

    def test_unmigrated_schema_degrades_per_field(self, tmp_path):
        c = sqlite3.connect(tmp_path / "old.db")
        c.row_factory = sqlite3.Row
        c.executescript("""
            CREATE TABLE orders(ts TEXT, symbol TEXT, side TEXT, sleeve TEXT,
                qty REAL, notional REAL, limit_price REAL, stop_price REAL,
                reason TEXT, alpaca_id TEXT, status TEXT);
            CREATE TABLE rejections(ts TEXT, symbol TEXT, reason TEXT);
        """)
        c.execute("INSERT INTO orders VALUES ('2026-08-01T09:47:00-04:00','A','buy','s',"
                  "1,100.0,101.0,0.0,'r','id','filled')")
        days = dashboard_db.execution_trends(c, "2026-07-31")
        assert days[0]["orders"] == 1
        assert days[0]["adverse_slippage_bps"] is None
        assert days[0]["avg_latency_s"] is None
        c.close()


class TestRoundTrips:
    def _order(self, conn, ts, symbol, side, qty, price, sleeve="mom_ls"):
        conn.execute(
            "INSERT INTO orders(ts, symbol, side, sleeve, qty, notional, status, "
            "filled_qty, filled_avg_price) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, symbol, side, sleeve, qty, qty * price, "filled", qty, price),
        )

    def test_complete_long_round_trip(self, conn):
        self._order(conn, "2026-08-01T10:00:00-04:00", "WDC", "buy", 1.0, 100.0)
        self._order(conn, "2026-08-03T10:00:00-04:00", "WDC", "sell", 1.0, 110.0)
        result = dashboard_db.round_trips(conn)
        assert len(result["trips"]) == 1
        assert result["trips"][0]["realized_pnl"] == 10.0
        assert result["unmatched"] == 0
        assert result["by_sleeve"]["mom_ls"]["wins"] == 1

    def test_short_round_trip_pnl_sign(self, conn):
        self._order(conn, "2026-08-01T10:00:00-04:00", "OLLI", "short", 1.0, 80.0)
        self._order(conn, "2026-08-03T10:00:00-04:00", "OLLI", "cover", 1.0, 75.0)
        result = dashboard_db.round_trips(conn)
        assert result["trips"][0]["realized_pnl"] == 5.0  # covered lower = profit

    def test_partial_fill_matched_pro_rata(self, conn):
        self._order(conn, "2026-08-01T10:00:00-04:00", "XOM", "buy", 2.0, 50.0)
        self._order(conn, "2026-08-03T10:00:00-04:00", "XOM", "sell", 1.0, 55.0)
        result = dashboard_db.round_trips(conn)
        assert len(result["trips"]) == 1
        assert result["trips"][0]["qty"] == 1.0
        assert result["trips"][0]["realized_pnl"] == 5.0

    def test_unmatched_exit_counted_not_guessed(self, conn):
        self._order(conn, "2026-08-03T10:00:00-04:00", "SPY", "sell", 1.0, 700.0)
        result = dashboard_db.round_trips(conn)
        assert result["trips"] == []
        assert result["unmatched"] == 1

    def test_fifo_order_of_lots(self, conn):
        self._order(conn, "2026-08-01T10:00:00-04:00", "AAA", "buy", 1.0, 100.0)
        self._order(conn, "2026-08-02T10:00:00-04:00", "AAA", "buy", 1.0, 200.0)
        self._order(conn, "2026-08-03T10:00:00-04:00", "AAA", "sell", 1.0, 150.0)
        result = dashboard_db.round_trips(conn)
        # FIFO: exit matches the first (100) lot -> +50, not the 200 lot
        assert result["trips"][0]["realized_pnl"] == 50.0

    def test_unmigrated_schema_returns_empty_shape(self, tmp_path):
        c = sqlite3.connect(tmp_path / "old.db")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE orders(ts TEXT, symbol TEXT, side TEXT, status TEXT)")
        result = dashboard_db.round_trips(c)
        assert result["trips"] == [] and result["by_sleeve"] == {}
        assert "coverage_note" in result
        c.close()


class TestExposureHistory:
    def test_one_point_per_day_last_write_wins(self, conn):
        conn.executemany(
            "INSERT INTO attribution_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-08-01T09:47:00-04:00", 10000, 0.8, 0.1, 0.9, 0.7, 0.1, 0.8,
                 "{}", "{}", "{}", "{}", "[]"),
                ("2026-08-01T12:35:00-04:00", 10000, 0.8, 0.1, 0.95, 0.75, 0.1, 0.85,
                 "{}", "{}", "{}", "{}", "[]"),
                ("2026-08-02T09:47:00-04:00", 10100, 0.8, 0.1, 0.9, 0.72, 0.1, 0.82,
                 "{}", "{}", "{}", "{}", "[]"),
            ],
        )
        history = dashboard_db.exposure_history(conn)
        assert len(history) == 2
        assert history[0]["target_gross"] == 0.95  # the later 08-01 write
        assert history[0]["target_net"] == pytest.approx(0.7)
        assert history[1]["actual_gross"] == 0.82


def test_trends_route(client):
    data = client.get("/api/base/trends?days=30").get_json()
    assert len(data["days"]) >= 2  # fixture has fills on two calendar days


def test_round_trips_route(client):
    data = client.get("/api/base/round-trips").get_json()
    symbols = {t["symbol"] for t in data["trips"]}
    assert "WDC" in symbols and "XOM" in symbols
    assert data["by_sleeve"]["mom_ls"]["trips"] == 2
    assert data["coverage_note"]


def test_exposure_route_carries_history(client):
    data = client.get("/api/base/exposure").get_json()
    assert isinstance(data["history"], list)
    assert len(data["history"]) == 1  # fixture has one attribution day


def test_equity_curve_route_carries_derivations(client):
    data = client.get("/api/base/equity-curve?days=90").get_json()
    last = data["points"][-1]
    assert "pnl" in last and "return_pct" in last and "drawdown_pct" in last
