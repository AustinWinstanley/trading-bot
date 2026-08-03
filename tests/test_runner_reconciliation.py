from __future__ import annotations

import sqlite3

from engine.risk import Position
from scripts.run_daily import (
    cancel_symbol_orders,
    is_liquidation_order,
    marketable_limit,
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


def test_only_dedicated_flatten_client_ids_are_liquidations():
    assert is_liquidation_order({"client_order_id": "bot-20260723-XLK-flatten"})
    assert not is_liquidation_order({"client_order_id": "bot-20260723-XLK-sell"})
    assert not is_liquidation_order({"client_order_id": "manual-flatten"})


def test_marketable_limit_rounds_inside_slippage_band():
    price = 739.73
    buy_limit = marketable_limit(price, "buy", 0.003)
    sell_limit = marketable_limit(price, "sell", 0.003)
    assert (buy_limit - price) / price <= 0.003
    assert (price - sell_limit) / price <= 0.003


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


def test_sync_retains_fractional_fallback_while_entry_is_pending():
    conn = journal()
    conn.execute(
        "INSERT INTO stops VALUES ('XLK', 92, 100, '2026-01-01', 'fractional-entry')"
    )
    sync_broker_stops(
        conn,
        positions={},
        open_orders=[{"symbol": "XLK", "type": "limit", "side": "buy"}],
        today=__import__("datetime").date.today(),
    )
    assert conn.execute("SELECT stop_price FROM stops WHERE symbol='XLK'").fetchone()[0] == 92


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


def test_report_keeps_every_run_of_the_day(tmp_path):
    """Two runs a day is normal; the second must not erase the first."""
    from scripts.run_daily import append_report

    report = tmp_path / "2026-08-03.md"
    append_report(report, "2026-08-03", ["## run 09:47", "- submitted 14"])
    append_report(report, "2026-08-03", ["## run 12:35", "- submitted 0"])

    body = report.read_text()
    assert body.count("# Paper 2026-08-03") == 1        # title written once
    assert "- submitted 14" in body                     # morning run survives
    assert "- submitted 0" in body
    assert body.index("09:47") < body.index("12:35")    # chronological
