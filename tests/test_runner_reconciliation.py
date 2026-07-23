from __future__ import annotations

import sqlite3

from engine.risk import Position
from scripts.run_daily import (
    cancel_symbol_orders,
    is_protective_order,
    reconcile_journal_orders,
    sync_broker_stops,
)


def journal() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE orders(ts, symbol, side, sleeve, qty, notional, "
        "limit_price, stop_price, reason, alpaca_id, status)"
    )
    conn.execute(
        "CREATE TABLE stops(symbol PRIMARY KEY, stop_price, entry_price, entry_date, sleeve)"
    )
    return conn


def test_stop_orders_are_protective_but_limits_are_pending_entries():
    assert is_protective_order({"type": "stop"})
    assert is_protective_order({"type": "stop_limit"})
    assert not is_protective_order({"type": "limit"})


def test_reconcile_updates_journal_from_broker():
    conn = journal()
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("t", "XLK", "buy", "clone", 1, 100, 100, 92, "", "abc", "accepted"),
    )

    class FakeTrader:
        def get_order(self, order_id):
            assert order_id == "abc"
            return {"status": "filled"}

    counts = reconcile_journal_orders(conn, FakeTrader())
    assert counts == {"filled": 1}
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "filled"


def test_sync_uses_most_protective_broker_stop_and_prunes_phantoms():
    conn = journal()
    conn.execute("INSERT INTO stops VALUES ('PHANTOM', 10, 12, '2026-01-01', 'x')")
    positions = {"XLK": Position("XLK", 2.0, 100.0, 105.0)}
    open_orders = [
        {"symbol": "XLK", "type": "stop", "stop_price": "92"},
        {"symbol": "XLK", "type": "stop", "stop_price": "95"},
    ]
    protected = sync_broker_stops(conn, positions, open_orders, __import__("datetime").date.today())
    assert protected == {"XLK"}
    assert conn.execute("SELECT stop_price FROM stops WHERE symbol='XLK'").fetchone()[0] == 95
    assert conn.execute("SELECT 1 FROM stops WHERE symbol='PHANTOM'").fetchone() is None


def test_cancel_symbol_orders_is_scoped_and_dry_run_safe():
    canceled = []

    class FakeTrader:
        def cancel_order(self, order_id):
            canceled.append(order_id)

    orders = [
        {"id": "a", "symbol": "XLK", "type": "stop"},
        {"id": "b", "symbol": "QQQ", "type": "limit"},
    ]
    assert cancel_symbol_orders(FakeTrader(), "XLK", orders, dry_run=True) == 1
    assert canceled == []
    assert cancel_symbol_orders(FakeTrader(), "XLK", orders, dry_run=False) == 1
    assert canceled == ["a"]
