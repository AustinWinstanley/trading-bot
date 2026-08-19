from __future__ import annotations

import sqlite3

import datetime as dt

from engine.risk import Position
import scripts.run_daily as runner
from scripts.run_daily import (
    broker_fill_fields,
    cancel_symbol_orders,
    is_liquidation_order,
    marketable_limit,
    is_protective_order,
    order_client_id,
    reconcile_journal_orders,
    stale_pending_orders,
    sync_broker_stops,
)


def journal() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE orders(ts, symbol, side, sleeve, qty, notional, "
        "limit_price, stop_price, reason, alpaca_id, status, "
        "requested_notional, reference_price, filled_qty, filled_avg_price, filled_at)"
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


def test_order_client_id_differs_across_same_day_resubmissions():
    """Real incident, 2026-08-18: a bare bot-YYYYMMDD-SYMBOL-SIDE id made a
    same-day resubmission of the same symbol+side (the stale-order
    cancel-and-reprice pass, or mom_ls adding to a name already bought that
    morning) collide with the earlier order and get 403/422 rejected by
    Alpaca as a duplicate client_order_id — silently dropping the order."""
    today = dt.date(2026, 8, 18)
    morning = dt.datetime(2026, 8, 18, 9, 51, 1, tzinfo=dt.timezone.utc)
    midday = dt.datetime(2026, 8, 18, 12, 39, 1, tzinfo=dt.timezone.utc)
    first = order_client_id(today, morning, "WING", "cover")
    second = order_client_id(today, midday, "WING", "cover")
    assert first != second
    # A regular order's id must never end in "-flatten" and be mistaken for
    # kill-switch liquidation by is_liquidation_order.
    assert not is_liquidation_order({"client_order_id": first})


def test_order_client_id_is_deterministic_within_one_run():
    """now_et is computed once per run (main()'s own module docstring),
    so two orders for different symbols in the same run must not collide
    with each other, and the same call must be reproducible for a fixed
    now_et — the property the original scheme actually needed."""
    today = dt.date(2026, 8, 18)
    now_et = dt.datetime(2026, 8, 18, 9, 51, 1, tzinfo=dt.timezone.utc)
    assert order_client_id(today, now_et, "WING", "cover") == order_client_id(
        today, now_et, "WING", "cover"
    )
    assert order_client_id(today, now_et, "WING", "cover") != order_client_id(
        today, now_et, "KLAC", "short"
    )


def test_order_client_id_stays_under_alpacas_48_char_limit():
    # Longest realistic symbol shape in this repo is an OCC option symbol
    # (see engine/options_risk.py); "short"/"cover" are the longest sides.
    today = dt.date(2026, 8, 18)
    now_et = dt.datetime(2026, 8, 18, 23, 59, 59, tzinfo=dt.timezone.utc)
    cid = order_client_id(today, now_et, "SPY260918P00751000", "cover")
    assert len(cid) <= 48


NOW = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)


def test_stale_pending_orders_flags_old_nonprotective_orders():
    """The BE/HUT 2026-08-13 case: a limit that went non-marketable right
    after the open run must be flagged for cancel-and-reprice once past
    the threshold."""
    orders = [
        {"id": "a", "symbol": "BE", "type": "limit", "side": "sell",
         "submitted_at": "2026-08-13T13:51:02Z"},          # 4h old -> stale
        {"id": "b", "symbol": "SPY", "type": "limit", "side": "buy",
         "submitted_at": "2026-08-13T17:45:00Z"},          # 15m old -> fresh
        {"id": "c", "symbol": "HYG", "type": "stop", "side": "sell",
         "submitted_at": "2026-07-23T13:00:00Z"},          # protective, never stale
    ]
    stale = stale_pending_orders(orders, NOW)
    assert [(o["symbol"], round(age)) for o, age in stale] == [("BE", 249)]


def test_stale_pending_orders_skips_unparsable_timestamps():
    orders = [
        {"id": "a", "symbol": "BE", "type": "limit", "submitted_at": None},
        {"id": "b", "symbol": "HUT", "type": "limit", "submitted_at": "not-a-time"},
    ]
    assert stale_pending_orders(orders, NOW) == []


def test_stale_pending_orders_threshold_boundary():
    orders = [{"id": "a", "symbol": "X", "type": "limit",
               "submitted_at": "2026-08-13T17:30:00Z"}]  # exactly 30m
    assert len(stale_pending_orders(orders, NOW, threshold_minutes=30)) == 1
    assert stale_pending_orders(orders, NOW, threshold_minutes=31) == []


def test_marketable_limit_rounds_inside_slippage_band():
    price = 739.73
    buy_limit = marketable_limit(price, "buy", 0.003)
    sell_limit = marketable_limit(price, "sell", 0.003)
    assert (buy_limit - price) / price <= 0.003
    assert (price - sell_limit) / price <= 0.003


def test_broker_fill_fields_tolerate_partial_and_malformed_payloads():
    assert broker_fill_fields({
        "filled_qty": "2.5",
        "filled_avg_price": "100.25",
        "filled_at": "t",
    }) == (2.5, 100.25, "t")
    assert broker_fill_fields({
        "filled_qty": "bad",
        "filled_avg_price": None,
    }) == (None, None, None)


def test_reconcile_updates_journal_from_broker():
    conn = journal()
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "t", "XLK", "buy", "clone", 1, 100, 100, 92, "", "abc",
            "accepted", 100, 99.5, None, None, None,
        ),
    )

    class FakeTrader:
        def get_order(self, order_id):
            assert order_id == "abc"
            return {
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "99.75",
                "filled_at": "2026-07-23T14:00:00Z",
            }

    counts = reconcile_journal_orders(conn, FakeTrader())
    assert counts == {"filled": 1}
    row = conn.execute(
        "SELECT status, filled_qty, filled_avg_price, filled_at FROM orders"
    ).fetchone()
    assert row == ("filled", 1.0, 99.75, "2026-07-23T14:00:00Z")


def test_reconcile_backfills_terminal_fills_missing_telemetry():
    conn = journal()
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "t", "XLK", "buy", "core", 1, 100, 100, 92, "", "abc",
            "filled", 100, 99.5, None, None, None,
        ),
    )

    class FakeTrader:
        def get_order(self, order_id):
            return {
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "99.75",
            }

    assert reconcile_journal_orders(conn, FakeTrader()) == {"filled": 1}
    assert conn.execute(
        "SELECT filled_avg_price FROM orders"
    ).fetchone()[0] == 99.75


def test_db_additively_migrates_legacy_journal(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE snapshots(ts, equity, cash, positions, diag);
    CREATE TABLE orders(
        ts, symbol, side, sleeve, qty, notional, limit_price, stop_price,
        reason, alpaca_id, status);
    CREATE TABLE rejections(ts, symbol, reason);
    CREATE TABLE stops(
        symbol PRIMARY KEY, stop_price, entry_price, entry_date, sleeve);
    INSERT INTO orders VALUES(
        't', 'SPY', 'buy', 'core', 1, 100, 100, 90, '', 'id', 'filled');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(runner, "DB", path)
    migrated = runner.db()
    order_columns = {
        row[1] for row in migrated.execute("PRAGMA table_info(orders)")
    }
    rejection_columns = {
        row[1] for row in migrated.execute("PRAGMA table_info(rejections)")
    }
    assert {"requested_notional", "reference_price", "filled_qty"}.issubset(
        order_columns
    )
    assert {"sleeve", "side", "requested_notional"}.issubset(
        rejection_columns
    )
    assert migrated.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    migrated.rollback()
    migrated.close()

    rolled_back = sqlite3.connect(path)
    rolled_back_columns = {
        row[1] for row in rolled_back.execute("PRAGMA table_info(orders)")
    }
    assert "requested_notional" not in rolled_back_columns
    assert rolled_back.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='attribution_snapshots'"
    ).fetchone() is None
    assert rolled_back.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='leverage_recommendations'"
    ).fetchone() is None
    rolled_back.close()

    persisted = runner.db()
    persisted.commit()
    persisted.close()
    reopened = sqlite3.connect(path)
    reopened_columns = {
        row[1] for row in reopened.execute("PRAGMA table_info(orders)")
    }
    assert "requested_notional" in reopened_columns
    assert reopened.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='leverage_recommendations'"
    ).fetchone() is not None


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
