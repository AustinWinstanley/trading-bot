"""Tests for the attention-signal layer on /summary — the "something needs
a look" states. Each signal has a real 2026-08 incident behind it (see the
docstrings in dashboard/db.py); these tests pin the exact conditions that
fire and, just as importantly, the quiet states that must not.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from dashboard import db as dashboard_db

NOW = dt.datetime(2026, 8, 14, 15, 0, tzinfo=dt.timezone.utc)  # Friday 11:00 ET


def _conn_with_orders(rows: list[tuple]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE orders(ts TEXT, symbol TEXT, status TEXT)")
    conn.executemany("INSERT INTO orders(ts, symbol, status) VALUES (?,?,?)", rows)
    return conn


class TestStuckNewOrders:
    def test_old_new_order_fires_danger(self):
        conn = _conn_with_orders([("2026-08-14T09:00:00+00:00", "HUT", "new")])
        signals = dashboard_db.stuck_new_orders(conn, NOW)
        assert len(signals) == 1
        assert signals[0]["severity"] == "danger"
        assert "HUT" in signals[0]["message"]

    def test_recent_new_order_is_quiet(self):
        conn = _conn_with_orders([("2026-08-14T14:50:00+00:00", "HUT", "new")])
        assert dashboard_db.stuck_new_orders(conn, NOW) == []

    def test_filled_orders_are_quiet(self):
        conn = _conn_with_orders([("2026-08-13T09:00:00+00:00", "SPY", "filled")])
        assert dashboard_db.stuck_new_orders(conn, NOW) == []

    def test_missing_status_column_is_quiet_not_an_error(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE orders(ts TEXT, symbol TEXT)")
        assert dashboard_db.stuck_new_orders(conn, NOW) == []


class TestHealthStaleness:
    def test_fresh_health_is_quiet(self):
        health = {"ts": "2026-08-14T10:00:00+00:00", "healthy": True}
        assert dashboard_db.health_staleness(health, NOW) == []

    def test_stale_health_fires_danger(self):
        health = {"ts": "2026-08-12T10:00:00+00:00", "healthy": True}
        signals = dashboard_db.health_staleness(health, NOW)
        assert len(signals) == 1
        assert signals[0]["severity"] == "danger"
        assert "stale" in signals[0]["message"]

    def test_no_health_file_is_quiet(self):
        assert dashboard_db.health_staleness(None, NOW) == []


class TestLastRunStaleness:
    def test_stale_during_market_hours_fires_warn(self):
        signals = dashboard_db.last_run_staleness("2026-08-14T04:00:00+00:00", NOW)
        assert len(signals) == 1
        assert signals[0]["severity"] == "warn"

    def test_fresh_during_market_hours_is_quiet(self):
        assert dashboard_db.last_run_staleness("2026-08-14T13:50:00+00:00", NOW) == []

    def test_weekend_is_always_quiet(self):
        saturday = dt.datetime(2026, 8, 15, 15, 0, tzinfo=dt.timezone.utc)
        assert dashboard_db.last_run_staleness("2026-08-10T09:00:00+00:00", saturday) == []

    def test_overnight_is_quiet(self):
        overnight = dt.datetime(2026, 8, 14, 6, 0, tzinfo=dt.timezone.utc)  # 02:00 ET
        assert dashboard_db.last_run_staleness("2026-08-13T16:39:00+00:00", overnight) == []


class TestMomLsTargetsStaleness:
    class _Cfg:
        def __init__(self, sleeves_paper):
            self.sleeves_paper = sleeves_paper

    _PAPER = {
        "mom_ls_targets_file": "state/mom_ls_targets.json",
        "mom_ls_max_age_days": 10,
        "sleeves": {"mom_ls": 0.15},
    }

    def test_missing_file_fires_danger(self, tmp_path: Path):
        (tmp_path / "state").mkdir()
        signals = dashboard_db.mom_ls_targets_staleness(tmp_path, self._Cfg(self._PAPER), NOW)
        assert len(signals) == 1
        assert signals[0]["id"] == "mom_ls_targets_missing"
        assert signals[0]["severity"] == "danger"

    def test_fresh_file_is_quiet(self, tmp_path: Path):
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "mom_ls_targets.json").write_text('{"as_of": "2026-08-13"}')
        assert dashboard_db.mom_ls_targets_staleness(tmp_path, self._Cfg(self._PAPER), NOW) == []

    def test_stale_file_fires_danger(self, tmp_path: Path):
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "mom_ls_targets.json").write_text('{"as_of": "2026-07-01"}')
        signals = dashboard_db.mom_ls_targets_staleness(tmp_path, self._Cfg(self._PAPER), NOW)
        assert len(signals) == 1
        assert signals[0]["id"] == "mom_ls_targets_stale"

    def test_profile_without_mom_ls_sleeve_is_quiet(self, tmp_path: Path):
        paper = {**self._PAPER, "sleeves": {}}
        assert dashboard_db.mom_ls_targets_staleness(tmp_path, self._Cfg(paper), NOW) == []


class TestOptionsDbZeroByte:
    def test_zero_byte_file_fires_warn(self, tmp_path: Path):
        db_path = tmp_path / "options.db"
        db_path.touch()
        paths = dashboard_db.ProfilePaths(
            profile="base", config_path=tmp_path / "c.yaml", db_path=tmp_path / "p.db",
            risk_state_path=tmp_path / "r.json", health_status_path=tmp_path / "h.json",
            options_db_path=db_path,
        )
        signals = dashboard_db.options_db_zero_byte(paths)
        assert len(signals) == 1
        assert signals[0]["severity"] == "warn"

    def test_missing_file_is_quiet(self, tmp_path: Path):
        paths = dashboard_db.ProfilePaths(
            profile="base", config_path=tmp_path / "c.yaml", db_path=tmp_path / "p.db",
            risk_state_path=tmp_path / "r.json", health_status_path=tmp_path / "h.json",
            options_db_path=tmp_path / "does-not-exist.db",
        )
        assert dashboard_db.options_db_zero_byte(paths) == []


class TestBuyingPowerMissSignals:
    def test_nonzero_streak_fires_warn(self):
        experiments = {"buying_power_misses": {"bull_put_delta_selected_live": 2}}
        signals = dashboard_db.buying_power_miss_signals(experiments)
        assert len(signals) == 1
        assert "2 consecutive" in signals[0]["message"]

    def test_zero_streak_is_quiet(self):
        experiments = {"buying_power_misses": {"bull_put_delta_selected_live": 0}}
        assert dashboard_db.buying_power_miss_signals(experiments) == []

    def test_absent_key_is_quiet(self):
        assert dashboard_db.buying_power_miss_signals({}) == []


def test_summary_route_carries_attention_signals(client):
    """The seeded fixture has one stuck 'new' order (HUT, from yesterday)
    and a fresh mom_ls targets file + fresh health, so exactly the
    stuck-order danger fires."""
    data = client.get("/api/base/summary").get_json()
    signals = data["attention"]["signals"]
    ids = [s["id"] for s in signals]
    assert "stuck_new_orders" in ids
    stuck = next(s for s in signals if s["id"] == "stuck_new_orders")
    assert stuck["severity"] == "danger"
    assert "HUT" in stuck["message"]
    # Quiet states that must NOT fire against the healthy fixture:
    assert "health_stale" not in ids
    assert "mom_ls_targets_missing" not in ids
    assert "mom_ls_targets_stale" not in ids


def test_summary_route_dangers_sort_before_warns(client):
    signals = client.get("/api/base/summary").get_json()["attention"]["signals"]
    severities = [s["severity"] for s in signals]
    assert severities == sorted(severities, key=lambda s: 0 if s == "danger" else 1)
