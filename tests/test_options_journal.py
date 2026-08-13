"""Journal-persistence tests for scripts/options_daily.py's state/options_2x.db."""

from __future__ import annotations

import datetime as dt
import sqlite3

from engine.execute import OptionLeg
from engine.options_risk import ApprovedOptionStructure, OptionLegQuote, OptionStructureProposal
from scripts.options_daily import db as options_db
from scripts.options_daily import fetch_open_structures, insert_structure, record_close_submission


def _connect(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.options_daily.DB", tmp_path / "options_2x.db")
    return options_db()


def _approved():
    return ApprovedOptionStructure(
        sleeve="bull_put_delta_selected_live", underlying="SPY",
        expiration_date=dt.date(2026, 9, 18),
        legs=(
            OptionLeg("SPY260918P00744000", "sell", "sell_to_open", 1),
            OptionLeg("SPY260918P00739000", "buy", "buy_to_open", 1),
        ),
        contracts=1, requested_contracts=1, credit=0.67, maximum_loss=473.0,
    )


def _proposal(now):
    return OptionStructureProposal(
        sleeve="bull_put_delta_selected_live", underlying="SPY",
        expiration_date=dt.date(2026, 9, 18),
        legs=(
            OptionLegQuote("SPY260918P00744000", "sell", "sell_to_open", 1, now, bid=4.46, ask=4.49),
            OptionLegQuote("SPY260918P00739000", "buy", "buy_to_open", 1, now, bid=3.77, ask=3.79),
        ),
        contracts=1, credit=0.67, maximum_loss=473.0,
    )


def test_insert_and_fetch_open_structure_round_trips(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    now = dt.datetime(2026, 8, 12, 15, 0, tzinfo=dt.timezone.utc)
    insert_structure(
        conn, _approved(), _proposal(now),
        structure_id="abc123", ts=now.isoformat(),
        order={"id": "alpaca-1", "client_order_id": "opt-1", "status": "accepted"},
    )
    conn.commit()

    open_structures = fetch_open_structures(conn, "bull_put_delta_selected_live")
    assert len(open_structures) == 1
    structure = open_structures[0]
    assert structure["structure_id"] == "abc123"
    assert structure["status"] == "open_pending"
    assert structure["contracts"] == 1
    assert len(structure["legs"]) == 2
    assert {leg["symbol"] for leg in structure["legs"]} == {
        "SPY260918P00744000", "SPY260918P00739000",
    }


def test_closed_structure_is_not_returned_as_open(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    now = dt.datetime(2026, 8, 12, 15, 0, tzinfo=dt.timezone.utc)
    insert_structure(
        conn, _approved(), _proposal(now),
        structure_id="abc123", ts=now.isoformat(),
        order={"id": "alpaca-1", "client_order_id": "opt-1", "status": "accepted"},
    )
    conn.execute("UPDATE structures SET status='closed' WHERE structure_id='abc123'")
    conn.commit()
    assert fetch_open_structures(conn, "bull_put_delta_selected_live") == []


def test_record_close_submission_moves_status_to_closing_pending(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    now = dt.datetime(2026, 8, 12, 15, 0, tzinfo=dt.timezone.utc)
    insert_structure(
        conn, _approved(), _proposal(now),
        structure_id="abc123", ts=now.isoformat(),
        order={"id": "alpaca-1", "client_order_id": "opt-1", "status": "accepted"},
    )
    conn.execute("UPDATE structures SET status='open' WHERE structure_id='abc123'")
    record_close_submission(
        conn, "abc123",
        order={"id": "alpaca-2", "client_order_id": "opt-1-close", "status": "accepted"},
        reason="close_by_dte",
    )
    conn.commit()
    row = conn.execute(
        "SELECT status, close_reason, close_alpaca_order_id FROM structures WHERE structure_id='abc123'"
    ).fetchone()
    assert row == ("closing_pending", "close_by_dte", "alpaca-2")


def test_additive_migration_preserves_existing_rows(tmp_path, monkeypatch):
    """Mirrors the _ensure_column pattern used throughout scripts/options_shadow.py
    — an already-deployed table gains new columns without losing rows."""
    db_path = tmp_path / "options_2x.db"
    monkeypatch.setattr("scripts.options_daily.DB", db_path)

    # Simulate an older deployment: the full current schema MINUS the two
    # columns db() adds via _ensure_column (open/close_filled_avg_price).
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE structures(
            structure_id TEXT PRIMARY KEY, experiment TEXT NOT NULL,
            strategy TEXT NOT NULL, underlying TEXT NOT NULL,
            expiration_date TEXT NOT NULL, contracts INTEGER NOT NULL,
            requested_contracts INTEGER NOT NULL, credit REAL NOT NULL,
            maximum_loss REAL NOT NULL, adjustments TEXT, opened_ts TEXT NOT NULL,
            open_client_order_id TEXT, open_alpaca_order_id TEXT,
            open_status TEXT, open_filled_at TEXT,
            close_reason TEXT, closed_ts TEXT,
            close_client_order_id TEXT, close_alpaca_order_id TEXT,
            close_status TEXT, close_filled_at TEXT,
            realized_pnl REAL, status TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO structures(structure_id, experiment, strategy, underlying, "
        "expiration_date, contracts, requested_contracts, credit, maximum_loss, "
        "adjustments, opened_ts, status) VALUES "
        "('old1','bull_put_delta_selected_live','bull_put_delta_selected','SPY',"
        "'2026-09-18',1,1,0.67,473.0,'[]','2026-08-01T00:00:00','open')"
    )
    conn.commit()
    conn.close()

    conn = options_db()  # current schema, run against the pre-existing db file
    row = conn.execute(
        "SELECT structure_id, status FROM structures WHERE structure_id='old1'"
    ).fetchone()
    assert row == ("old1", "open")
    # The migrated columns exist and are queryable (NULL for the
    # pre-existing row) without having lost that row.
    fill_row = conn.execute(
        "SELECT open_filled_avg_price, close_filled_avg_price FROM structures "
        "WHERE structure_id='old1'"
    ).fetchone()
    assert fill_row == (None, None)
