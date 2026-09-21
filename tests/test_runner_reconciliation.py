from __future__ import annotations

import sqlite3

import datetime as dt

from engine.config import load_config
from engine.risk import Position
import scripts.run_daily as runner
from scripts.run_daily import (
    backfill_missing_stops,
    broker_fill_fields,
    cancel_symbol_orders,
    is_liquidation_order,
    prune_exempt_stops,
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


def test_stale_pending_orders_never_flags_an_options_order():
    """2026-09-14: the 12:39 daily2x run canceled the bull-put spread's
    close_by_dte order as "stale" and could not re-price it; the spread
    rode unmanaged through expiration. An mleg order has no top-level
    symbol or side — this is the shape Alpaca actually returns."""
    orders = [
        {"id": "m", "symbol": None, "type": "limit", "order_class": "mleg",
         "asset_class": "", "limit_price": "0.53",
         "legs": [{"symbol": "SPY260918P00751000"}, {"symbol": "SPY260918P00746000"}],
         "submitted_at": "2026-08-13T14:05:01Z"},
        {"id": "s", "symbol": "SPY260918P00751000", "type": "limit",
         "asset_class": "us_option", "submitted_at": "2026-08-13T14:05:01Z"},
    ]
    assert stale_pending_orders(orders, NOW) == []


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


class FakeBarsTrader:
    """get_bars returns no history for every symbol — exercises the ATR
    fallback (price * 0.02) without needing a real pandas fixture."""

    def get_bars(self, symbols, start, end, timeframe="1Day", adjustment="all"):
        return {}


def test_backfill_establishes_a_stop_for_a_position_that_never_had_one():
    """Real incident, 2x profile: GLD's fractional buy on 2026-07-23 got
    neither a broker bracket stop nor a software fallback row, and sat
    unprotected for a month until healthcheck.py caught it on 2026-08-20.
    A position with no order history (held_sleeve has no entry for it) must
    still be conservatively backfilled, not skipped."""
    conn = journal()
    positions = {"GLD": Position("GLD", 0.323557, 372.18, 423.705)}
    raw_positions = [{"symbol": "GLD", "asset_class": "us_equity"}]
    backfilled = backfill_missing_stops(
        conn, FakeBarsTrader(), load_config(), positions, raw_positions,
        held_sleeve={}, today=dt.date(2026, 8, 24),
    )
    assert backfilled == ["GLD"]
    stop_price, origin = conn.execute(
        "SELECT stop_price, sleeve FROM stops WHERE symbol='GLD'"
    ).fetchone()
    assert origin == "backfill"
    # ATR fallback (2% of price) is narrower than config.yaml's stop_loss_pct
    # floor (8%), so the floor wins: stop_distance_pct(cfg, ...) == 0.08.
    assert stop_price == round(423.705 * 0.92, 4)


def test_backfill_skips_stop_exempt_sleeves():
    conn = journal()
    positions = {"FIG": Position("FIG", -8.0, 25.83, 27.235)}
    raw_positions = [{"symbol": "FIG", "asset_class": "us_equity"}]
    backfilled = backfill_missing_stops(
        conn, FakeBarsTrader(), load_config(), positions, raw_positions,
        held_sleeve={"FIG": "mom_ls"}, today=dt.date(2026, 8, 24),
    )
    assert backfilled == []
    assert conn.execute("SELECT 1 FROM stops WHERE symbol='FIG'").fetchone() is None


def test_backfill_skips_positions_that_already_have_a_stop():
    conn = journal()
    conn.execute(
        "INSERT INTO stops VALUES ('GLD', 400.0, 372.18, '2026-08-20', 'broker')"
    )
    positions = {"GLD": Position("GLD", 0.323557, 372.18, 423.705)}
    raw_positions = [{"symbol": "GLD", "asset_class": "us_equity"}]
    backfilled = backfill_missing_stops(
        conn, FakeBarsTrader(), load_config(), positions, raw_positions,
        held_sleeve={"GLD": "tsmom"}, today=dt.date(2026, 8, 24),
    )
    assert backfilled == []
    assert conn.execute(
        "SELECT stop_price FROM stops WHERE symbol='GLD'"
    ).fetchone()[0] == 400.0


def test_backfill_skips_option_legs():
    conn = journal()
    positions = {
        "SPY260918P00751000": Position("SPY260918P00751000", -1.0, 5.63, 5.63),
    }
    raw_positions = [
        {"symbol": "SPY260918P00751000", "asset_class": "us_option"},
    ]
    backfilled = backfill_missing_stops(
        conn, FakeBarsTrader(), load_config(), positions, raw_positions,
        held_sleeve={}, today=dt.date(2026, 8, 24),
    )
    assert backfilled == []


def test_backfill_puts_a_short_stop_above_price():
    conn = journal()
    positions = {"BMNR": Position("BMNR", -9.12758, 21.58, 22.75)}
    raw_positions = [{"symbol": "BMNR", "asset_class": "us_equity"}]
    backfilled = backfill_missing_stops(
        conn, FakeBarsTrader(), load_config(), positions, raw_positions,
        held_sleeve={"BMNR": "tsmom"}, today=dt.date(2026, 8, 24),
    )
    assert backfilled == ["BMNR"]
    stop_price = conn.execute(
        "SELECT stop_price FROM stops WHERE symbol='BMNR'"
    ).fetchone()[0]
    assert stop_price > 22.75


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


# --------------------------------------------------------------------------
# Rebalance threshold — fraction-of-target band, capped as a share of equity
# --------------------------------------------------------------------------


def test_rebalance_threshold_small_slot_is_governed_by_min_notional():
    from scripts.run_daily import rebalance_threshold
    # $75 mom_ls-sized slot: 20% band = $15, overridden by the $25 minimum.
    assert rebalance_threshold(75.0, 10_000.0, band=0.20, band_cap=0.05,
                               min_notional=25.0, full_exit=False) == 25.0


def test_rebalance_threshold_large_sleeve_is_capped_at_equity_fraction():
    from scripts.run_daily import rebalance_threshold
    # SPY at a 75% target on $10k: 20% of target = $1,500, capped at 5% of
    # equity = $500 so a target change cannot leave 15% of equity idle.
    assert rebalance_threshold(7_500.0, 10_000.0, band=0.20, band_cap=0.05,
                               min_notional=25.0, full_exit=False) == 500.0


def test_rebalance_threshold_without_cap_keeps_fractional_band():
    from scripts.run_daily import rebalance_threshold
    assert rebalance_threshold(7_500.0, 10_000.0, band=0.20, band_cap=None,
                               min_notional=25.0, full_exit=False) == 1_500.0


def test_rebalance_threshold_full_exit_always_trades():
    from scripts.run_daily import rebalance_threshold
    # A held position with no target is exited regardless of size — the
    # sub-$25 fractional-dust remnants must clear.
    assert rebalance_threshold(0.0, 10_000.0, band=0.20, band_cap=0.05,
                               min_notional=25.0, full_exit=True) == 0.0


# --------------------------------------------------------------------------
# Order journal durability — every broker-order write must commit
# immediately, not wait for main()'s single end-of-run commit
# --------------------------------------------------------------------------


def test_order_writes_commit_immediately_not_deferred_to_end_of_run():
    """Regression for the 2026-09-15 false 'possible option assignment'
    CRITICAL: a 59-order 2x rotation left every fill uncommitted (SQLite
    transactions are all-or-nothing across connections) until main()'s
    single end-of-run commit. health2x opened its own read-only connection
    8 minutes into that still-running daily2x job and legitimately saw
    none of that run's orders, so equity_qty_explained_by_orders summed
    only stale prior-day rows; the mismatch against the broker's
    already-updated position read as a possible option assignment and
    made options_daily2x skip its entry pass. Worse than any single day's
    false positive: a crash after real broker fills but before the old
    end-of-run commit would have rolled those fills back out of the local
    journal entirely, with the broker's account left in a state the
    journal had no record of at all.

    Each of the three broker-order-write sites (kill-switch flatten,
    submission success, submission failure) must commit before the loop
    can make another broker call, so a fill is durable and visible to any
    other connection (dashboard, MCP server, healthcheck, options_daily)
    within milliseconds, not for the remainder of a potentially
    multi-minute run.
    """
    import inspect

    src = inspect.getsource(runner)
    sites = {
        "kill-switch flatten": 'result.halt_reason or "flatten", o.get("id"),',
        "submission success": "succeeded_orders.append(order)",
        "submission failure": 'f"submit failed: {str(exc)[:200]}", None, "submit_failed",',
    }
    for label, marker in sites.items():
        idx = src.index(marker)
        window = src[idx: idx + 1500]  # generous: covers the explanatory comment above each commit()
        assert "conn.commit()" in window, (
            f"{label} order write has no conn.commit() shortly after it — "
            "a fill submitted here would sit uncommitted (invisible to every "
            "other reader) until the run's final commit"
        )


def test_backfill_does_not_stop_the_core_over_a_stood_down_sleeve():
    """2x, 2026-09-15 and again 2026-09-21: SPY's last BUY was journaled as
    `equity_core+trend` on 2026-08-04; `trend` has been 0.0 since
    2026-09-14. A trim deleted the stop row, exact-match exemption read the
    stale combined sleeve as "stopped", and the next run backfilled an -8%
    software stop onto a position that is ~85% of the account and that the
    M6 study models with no stop at all."""
    conn = journal()
    cfg = load_config()
    assert cfg.sleeves_paper["sleeves"]["trend"] == 0.0  # the premise
    positions = {"SPY": Position("SPY", 9.77, 756.82, 773.06)}
    raw_positions = [{"symbol": "SPY", "asset_class": "us_equity"}]
    backfilled = backfill_missing_stops(
        conn, FakeBarsTrader(), cfg, positions, raw_positions,
        held_sleeve={"SPY": "equity_core+trend"}, today=dt.date(2026, 9, 21),
    )
    assert backfilled == []
    assert conn.execute("SELECT 1 FROM stops WHERE symbol='SPY'").fetchone() is None


def _stop(conn, symbol, price=703.2572, origin="fractional-entry"):
    conn.execute("INSERT INTO stops VALUES (?,?,?,?,?)",
                 (symbol, price, 764.41, "2026-08-04", origin))


def _stopped(conn):
    return sorted(r[0] for r in conn.execute("SELECT symbol FROM stops"))


def test_prune_removes_a_stop_that_predates_its_sleeves_exemption():
    """Base, 2026-09-21: SPY still carried the 2026-08-04 stop from when it
    was part `trend`. Triggered, it sells the M6 core at -8% and the
    re-entry-exempt sleeve buys it back the next run."""
    conn = journal()
    _stop(conn, "SPY")
    positions = {"SPY": Position("SPY", 10.1474, 764.41, 767.08)}
    pruned = prune_exempt_stops(
        conn, load_config(), positions, {"SPY": "equity_core"}, protective_symbols=set()
    )
    assert pruned == ["SPY"]
    assert _stopped(conn) == []


def test_prune_reads_a_stale_combined_sleeve_the_way_backfill_does():
    conn = journal()
    _stop(conn, "SPY")
    positions = {"SPY": Position("SPY", 9.77, 756.82, 773.06)}
    assert prune_exempt_stops(
        conn, load_config(), positions, {"SPY": "equity_core+trend"}, set()
    ) == ["SPY"]
    # ... and backfill must not put it straight back.
    assert backfill_missing_stops(
        conn, FakeBarsTrader(), load_config(), positions,
        [{"symbol": "SPY", "asset_class": "us_equity"}],
        held_sleeve={"SPY": "equity_core+trend"}, today=dt.date(2026, 9, 21),
    ) == []


def test_prune_keeps_every_stop_it_cannot_prove_is_exempt():
    conn = journal()
    for symbol in ("XLK", "GLD", "SPY", "QLD", "OLD"):
        _stop(conn, symbol)
    positions = {
        s: Position(s, 1.0, 100.0, 100.0) for s in ("XLK", "GLD", "SPY", "QLD")
    }
    held_sleeve = {
        "XLK": "tsmom",        # a stopped sleeve
        # GLD: no journal attribution at all -> unknown means "needs a stop"
        "SPY": "equity_core",  # exempt, but the broker holds a live stop order
        "QLD": "lev_trend",    # exempt, software row -> the only one pruned
        "OLD": "equity_core",  # exempt but no longer held: not this function's row
    }
    pruned = prune_exempt_stops(
        conn, load_config(), positions, held_sleeve, protective_symbols={"SPY"}
    )
    assert pruned == ["QLD"]
    assert _stopped(conn) == ["GLD", "OLD", "SPY", "XLK"]
