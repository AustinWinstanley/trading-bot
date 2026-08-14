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

# Mirrors scripts/options_daily.py::db()'s schema (structures +
# structure_legs + reconciliation_events) — keep in sync with that module.
OPTIONS_SCHEMA = """
CREATE TABLE structures(
    structure_id TEXT PRIMARY KEY, experiment TEXT NOT NULL,
    strategy TEXT NOT NULL, underlying TEXT NOT NULL,
    expiration_date TEXT NOT NULL, contracts INTEGER NOT NULL,
    requested_contracts INTEGER NOT NULL, credit REAL NOT NULL,
    maximum_loss REAL NOT NULL, adjustments TEXT, opened_ts TEXT NOT NULL,
    open_client_order_id TEXT, open_alpaca_order_id TEXT,
    open_status TEXT, open_filled_at TEXT, open_filled_avg_price REAL,
    close_reason TEXT, closed_ts TEXT,
    close_client_order_id TEXT, close_alpaca_order_id TEXT,
    close_status TEXT, close_filled_at TEXT, close_filled_avg_price REAL,
    realized_pnl REAL, status TEXT NOT NULL);
CREATE TABLE structure_legs(
    structure_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    position_intent TEXT NOT NULL, ratio_qty INTEGER NOT NULL,
    quote_bid REAL, quote_ask REAL, quote_ts TEXT,
    PRIMARY KEY (structure_id, symbol));
CREATE TABLE reconciliation_events(
    ts TEXT NOT NULL, severity TEXT NOT NULL, structure_id TEXT,
    symbol TEXT, detail TEXT);
"""


def build_options_db(db_path):
    today = dt.date.today()
    ts = f"{today.isoformat()}T10:05:00-04:00"
    conn = sqlite3.connect(db_path)
    conn.executescript(OPTIONS_SCHEMA)
    conn.execute(
        "INSERT INTO structures(structure_id, experiment, strategy, underlying, "
        "expiration_date, contracts, requested_contracts, credit, maximum_loss, "
        "adjustments, opened_ts, open_status, status) "
        "VALUES ('abc123', 'bull_put_delta_selected_live', 'bull_put_delta_selected', "
        "'SPY', '2026-09-18', 1, 1, 0.67, 473.0, '[]', ?, 'accepted', 'open')",
        (ts,),
    )
    conn.executemany(
        "INSERT INTO structure_legs(structure_id, symbol, side, position_intent, "
        "ratio_qty) VALUES (?,?,?,?,?)",
        [
            ("abc123", "SPY260918P00744000", "sell", "sell_to_open", 1),
            ("abc123", "SPY260918P00739000", "buy", "buy_to_open", 1),
        ],
    )
    conn.execute(
        "INSERT INTO reconciliation_events VALUES (?,?,?,?,?)",
        (ts, "CRITICAL", "abc123", "SPY",
         "unexpected equity position in SPY while an options structure is open"),
    )
    conn.commit()
    conn.close()

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
            # A limit order stuck at status='new' since yesterday — feeds the
            # stuck_new_orders attention signal (the real BE/HUT case of
            # 2026-08-13, where two orders sat 'new' all day unnoticed).
            (f"{dates[1]}T09:47:03-04:00", "HUT", "sell", "rebalance", 1, 91.0,
             91.5, 0.0, "clean", "id-3", "new", 91.0, 91.0, 0.0, None, None),
            # A complete round trip (WDC: buy day 0, sell day 2) plus a
            # partial exit (XOM: buy 2, sell 1) — feeds round_trips()'s
            # FIFO matching tests, and gives execution_trends() fills on
            # more than one calendar day.
            (f"{dates[0]}T09:47:04-04:00", "WDC", "buy", "mom_ls", 1, 100.0,
             101.0, 0.0, "clean", "id-4", "filled", 100.0, 100.0, 1.0, 100.0,
             f"{dates[0]}T09:47:08-04:00"),
            (f"{dates[2]}T09:47:05-04:00", "WDC", "sell", "mom_ls", 1, 110.0,
             109.0, 0.0, "clean", "id-5", "filled", 110.0, 110.0, 1.0, 110.0,
             f"{dates[2]}T09:47:09-04:00"),
            (f"{dates[0]}T09:47:06-04:00", "XOM", "buy", "mom_ls", 2, 100.0,
             51.0, 0.0, "clean", "id-6", "filled", 100.0, 50.0, 2.0, 50.0,
             f"{dates[0]}T09:47:10-04:00"),
            (f"{dates[2]}T09:47:07-04:00", "XOM", "sell", "mom_ls", 1, 55.0,
             54.0, 0.0, "clean", "id-7", "filled", 55.0, 55.0, 1.0, 55.0,
             f"{dates[2]}T09:47:11-04:00"),
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
            '{"equity_core+trend": {"long": 0.60, "short": 0.0, "net": 0.60, '
            '"gross": 0.60}, "mom_ls": {"long": 0.15, "short": 0.15, "net": 0.0, '
            '"gross": 0.30}}',
            '{"equity_core+trend": {"long": 0.58, "short": 0.0, "net": 0.58, '
            '"gross": 0.58, "unrealized_pl": 300.0}, "mom_ls": {"long": 0.10, '
            '"short": 0.10, "net": 0.0, "gross": 0.20, "unrealized_pl": 20.0}}',
            # "weight_gap", matching engine/attribution.py's actual emitted
            # key — the fixture previously said "gap", which is exactly why
            # the NaN drift-line bug survived the test suite.
            "{}", "{}", '[{"symbol": "FXE", "weight_gap": -0.0354}]',
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


# Real schema of scripts/options_shadow.py's observations table (the one
# collector DB that exists in production) — keep in sync with that module.
# The other three collectors' fixtures only need (ts, expiration_date)-ish
# columns for count/staleness logic, so they reuse this minimal shape.
def build_shadow_db(db_path: Path, observation_ts: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE observations(ts TEXT, expiration_date TEXT, "
        "short_delta REAL, executable_credit REAL, qualified INTEGER, raw TEXT)"
    )
    conn.execute(
        "INSERT INTO observations VALUES (?, '2026-09-18', 0.2, 0.61, 1, '{\"big\": 1}')",
        (observation_ts,),
    )
    conn.commit()
    conn.close()


def build_reports_tree(repo_root: Path) -> None:
    reports = repo_root / "reports"
    (reports / "experiments").mkdir(parents=True)
    (reports / "experiments" / "bull_put_delta_selected_live.json").write_text(
        '{"decision": "promote_paper_active", '
        '"evidence_at_promotion_2026_08_12": {"shadow_review_bar": '
        '{"minimum_observations_before_review": 20, '
        '"minimum_independent_expirations_before_review": 3}}}'
    )
    (reports / "paper").mkdir()
    (reports / "paper" / "2026-08-12.md").write_text(
        "# Paper run 2026-08-12\n\napproved 1 | submitted 1\nHALT: test halt reason\n"
    )
    (reports / "paper_2x").mkdir()


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
        f'"recent_losses": {{"XYZ": "{loss_date}"}}, "halted": false, '
        '"experiment_realized_pnl": {"bull_put_delta_selected_live": -12.5}, '
        '"experiment_standdowns": ["stood_down_exp"]}'
    )
    # The base profile gets an options DB in tests even though production's
    # lives only in the 2x lab today — the endpoint is profile-generic and
    # the seeded-base/empty-2x split already covers both data states.
    build_options_db(state / "options.db")
    # Fresh mom_ls targets file (config.yaml's mom_ls_targets_file) so the
    # default fixture doesn't fire the mom_ls_targets_missing attention
    # signal — tests that want that signal delete or age this file.
    (state / "mom_ls_targets.json").write_text(
        f'{{"as_of": "{today.isoformat()}", "long": ["AAOI"], "short": ["XYZ"]}}'
    )
    (state / "health_status.json").write_text(
        f'{{"ts": "{today.isoformat()}T09:47:08-04:00", "healthy": true, "problems": [], '
        '"equity": 10500.0, "positions": 2, "open_orders": 0}'
    )
    # One fresh shadow-collector DB (the other three stay absent —
    # mirroring the live host, where only options_shadow has data),
    # the reports/ tree, and an empty logs/ dir (endpoints must treat
    # a missing log file as the normal quiet state).
    build_shadow_db(state / "options_shadow.db", f"{today.isoformat()}T15:07:00-04:00")
    build_reports_tree(tmp_path)
    (tmp_path / "logs").mkdir()
    return tmp_path


@pytest.fixture
def client(repo_root: Path):
    app = create_app(repo_root=repo_root)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
