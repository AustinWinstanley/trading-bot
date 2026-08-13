"""Shared fixtures for mcp_server tests.

Deliberately duplicates (rather than imports) the journal-building helpers
in tests/dashboard/conftest.py: tests/ has no __init__.py and relative
imports between test modules aren't reliable under pytest's default
rootdir import mode — see tests/dashboard/test_routes.py's own comment
for the same call made there. Keep this in sync by hand if that file's
SCHEMA/_build_journal/build_options_db change.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from pathlib import Path

import pytest

from engine.config import REPO_ROOT as REAL_REPO_ROOT
from mcp_server.app import create_server

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
    conn.execute(
        "INSERT INTO stops VALUES (?,?,?,?,?)",
        ("SPY", 620.0, 700.0, dates[0], "broker"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A fake repo root, extending tests/dashboard/conftest.py's shape
    with logs/ and reports/ — the two new mounts this package adds."""
    shutil.copy(REAL_REPO_ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy(REAL_REPO_ROOT / "config_2x.yaml", tmp_path / "config_2x.yaml")

    state = tmp_path / "state"
    state.mkdir()
    _build_journal(state / "paper.db")
    today = dt.date.today()
    (state / "risk_state.json").write_text(
        '{"peak_equity": 10500.0, "month": "2026-08", "month_start_equity": 10000.0, '
        f'"day": "{today.isoformat()}", "day_start_equity": 10400.0, '
        '"recent_losses": {}, "halted": false}'
    )
    (state / "health_status.json").write_text(
        f'{{"ts": "{today.isoformat()}T09:47:08-04:00", "healthy": true, "problems": [], '
        '"equity": 10500.0, "positions": 2, "open_orders": 0}'
    )

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "sample_study.json").write_text('{"verdict": "reject", "reason": "test fixture"}')
    experiments = reports / "experiments"
    experiments.mkdir()
    (experiments / "sample_live.json").write_text('{"hypothesis": "test fixture"}')
    paper_notes = reports / "paper"
    paper_notes.mkdir()
    (paper_notes / "2026-08-13.md").write_text("# 2026-08-13\n\ntest fixture note\n")

    logs = tmp_path / "logs"
    logs.mkdir()
    today_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    (logs / f"paper-{today_utc}.log").write_text(
        "=== 2026-08-13 13:47:00 UTC job=daily slot=09:47 ===\n"
        "done: filled 2 orders\n"
        "=== end rc=0 ===\n"
    )

    return tmp_path


@pytest.fixture
def mcp_server(repo_root: Path):
    return create_server(repo_root=repo_root)
