"""Shared fixtures for dashboard tests.

Builds a real temp-file SQLite journal (not :memory:, since dashboard.db's
mode=ro URI connections need an actual path) with the full current schema,
following the same inline CREATE TABLE pattern as
tests/test_attribution.py::_journal() — kept as a real file specifically so
these tests exercise the same open_ro() codepath production uses.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from pathlib import Path

import pytest

from dashboard.app import create_app
from engine.config import REPO_ROOT as REAL_REPO_ROOT

SCHEMA = """
CREATE TABLE snapshots(
    ts TEXT, equity REAL, cash REAL, positions TEXT, diag TEXT);
CREATE TABLE orders(
    ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, qty REAL, notional REAL,
    limit_price REAL, stop_price REAL, reason TEXT, alpaca_id TEXT, status TEXT,
    requested_notional REAL, reference_price REAL, filled_qty REAL,
    filled_avg_price REAL, filled_at TEXT);
CREATE TABLE rejections(
    ts TEXT, symbol TEXT, reason TEXT, sleeve TEXT, side TEXT,
    requested_notional REAL);
CREATE TABLE stops(
    symbol TEXT PRIMARY KEY, stop_price REAL, entry_price REAL, entry_date TEXT, sleeve TEXT);
CREATE TABLE attribution_snapshots(
    ts TEXT, equity REAL,
    target_long REAL, target_short REAL, target_gross REAL,
    actual_long REAL, actual_short REAL, actual_gross REAL,
    target_by_sleeve TEXT, actual_by_sleeve TEXT,
    targets TEXT, actual_weights TEXT, largest_symbol_gaps TEXT);
CREATE TABLE leverage_recommendations(
    ts TEXT, profile TEXT, mode TEXT, observations INTEGER,
    target_vol REAL, realized_vol REAL, recommended_scale REAL,
    recommended_leverage REAL, ready INTEGER, reason TEXT);
"""

def _build_journal(db_path: Path) -> None:
    today = dt.date.today()
    dates = [(today - dt.timedelta(days=offset)).isoformat() for offset in (2, 1, 0)]
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    conn.executemany(
        "INSERT INTO snapshots VALUES (?,?,?,?,?)",
        [
            (f"{dates[0]}T09:47:00-04:00", 10000.0, 3000.0,
             '{"SPY": {"qty": 10, "px": 700.0}, "AAOI": {"qty": 5, "px": 100.0}}',
             '{"origin": {"SPY": "equity_core+trend", "AAOI": "mom_ls"}}'),
            (f"{dates[1]}T09:47:00-04:00", 10200.0, 3200.0,
             '{"SPY": {"qty": 10, "px": 714.0}, "AAOI": {"qty": 5, "px": 102.0}}',
             '{"origin": {"SPY": "equity_core+trend", "AAOI": "mom_ls"}}'),
            (f"{dates[2]}T09:47:00-04:00", 10500.0, 3500.0,
             '{"SPY": {"qty": 10, "px": 730.0}, "AAOI": {"qty": 5, "px": 104.0}}',
             '{"origin": {"SPY": "equity_core+trend", "AAOI": "mom_ls"}}'),
        ],
    )
    conn.executemany(
        "INSERT INTO orders(ts, symbol, side, sleeve, qty, notional, limit_price, "
        "stop_price, reason, alpaca_id, status, requested_notional, "
        "reference_price, filled_qty, filled_avg_price, filled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (f"{dates[2]}T09:47:01-04:00", "SPY", "buy", "equity_core+trend", 1, 730.0,
             731.0, 0.0, "clean", "id-1", "filled", 730.0, 730.0, 1.0, 730.5,
             f"{dates[2]}T09:47:05-04:00"),
            (f"{dates[2]}T09:47:02-04:00", "AAOI", "buy", "mom_ls", 1, 104.0,
             105.0, 0.0, "clean", "id-2", "filled", 104.0, 104.0, 1.0, 104.2,
             f"{dates[2]}T09:47:06-04:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO rejections(ts, symbol, reason, sleeve, side, requested_notional) "
        "VALUES (?,?,?,?,?,?)",
        [
            (f"{dates[2]}T09:47:00-04:00", "FOO", "price 3.21 below minimum 5.00", "mom_ls", "buy", 75.0),
            (f"{dates[2]}T09:47:00-04:00", "BAR", "price 4.10 below minimum 5.00", "mom_ls", "buy", 75.0),
            (f"{dates[2]}T09:47:00-04:00", "BAZ", "short notional 12.30 rounds below 1 whole share", "mom_ls", "short", 75.0),
        ],
    )
    conn.execute(
        "INSERT INTO stops VALUES (?,?,?,?,?)",
        ("SPY", 620.0, 700.0, dates[0], "broker"),
    )
    conn.execute(
        "INSERT INTO attribution_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{dates[2]}T09:47:07-04:00", 10500.0,
            0.6, 0.15, 0.75, 0.58, 0.10, 0.68,
            "{}",
            '{"equity_core+trend": {"long": 0.58, "short": 0.0, "net": 0.58, '
            '"gross": 0.58, "unrealized_pl": 300.0}, "mom_ls": {"long": 0.10, '
            '"short": 0.10, "net": 0.0, "gross": 0.20, "unrealized_pl": 20.0}}',
            "{}", "{}", "[]",
        ),
    )
    conn.execute(
        "INSERT INTO leverage_recommendations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"{dates[2]}T09:47:07-04:00", "base", "shadow", 40,
            0.12, 0.15, 0.8, 0.8, 1, "shadow recommendation only",
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A fake repo root: real config files, a seeded journal, risk state.

    The 2x profile deliberately gets a config but no journal/state — that
    exercises the "profile configured but no data yet" path.
    """
    shutil.copy(REAL_REPO_ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy(REAL_REPO_ROOT / "config_2x.yaml", tmp_path / "config_2x.yaml")

    state = tmp_path / "state"
    state.mkdir()
    _build_journal(state / "paper.db")
    today = dt.date.today()
    loss_date = (today - dt.timedelta(days=2)).isoformat()
    (state / "risk_state.json").write_text(
        '{"peak_equity": 10500.0, "month": "2026-08", "month_start_equity": 10000.0, '
        f'"day": "{today.isoformat()}", "day_start_equity": 10400.0, '
        f'"recent_losses": {{"XYZ": "{loss_date}"}}, "halted": false}}'
    )
    (state / "health_status.json").write_text(
        f'{{"ts": "{today.isoformat()}T09:47:08-04:00", "healthy": true, "problems": [], '
        '"equity": 10500.0, "positions": 2, "open_orders": 0}'
    )
    return tmp_path


@pytest.fixture
def client(repo_root: Path):
    app = create_app(repo_root=repo_root)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
