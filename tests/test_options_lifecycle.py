"""Tests for the parts of scripts/options_daily.py that move a structure
out of a pending or expired state. All three gaps were live on the first
structure this repo ever closed (f927d483, 2026-09-14 .. 2026-09-21): its
close order was canceled unfilled and nothing noticed, the close had been
priced at the original credit, and its expiry was never journaled.
"""

from __future__ import annotations

import datetime as dt

import pytest

from scripts.options_daily import (
    closing_debit,
    expired_worthless,
    fetch_open_structures,
    finished_at_broker,
    insert_structure,
    occ_strike,
    reconcile_pending_orders,
    record_close_submission,
    settle_expired_structures,
)
from tests.test_options_journal import _approved, _connect, _proposal

NOW = dt.datetime(2026, 9, 14, 14, 5, tzinfo=dt.timezone.utc)
EXPERIMENT = "bull_put_delta_selected_live"
SHORT, LONG = "SPY260918P00744000", "SPY260918P00739000"


class FakeTrader:
    trading_base = "trading"
    data_base = "data"

    def __init__(self, orders=None, activities=None):
        self.orders = orders or {}
        self.activities = activities or []

    def get_order(self, order_id):
        return self.orders[order_id]

    def _get(self, base, path, params=None):
        assert path == "/v2/account/activities"
        return self.activities


def _open_structure(conn, *, open_fill_px=-0.60):
    insert_structure(
        conn, _approved(), _proposal(NOW), structure_id="abc123", ts=NOW.isoformat(),
        order={"id": "alpaca-open", "client_order_id": "opt-open", "status": "accepted"},
    )
    conn.execute(
        "UPDATE structures SET status='open', open_filled_avg_price=? "
        "WHERE structure_id='abc123'", (open_fill_px,),
    )


def _status(conn):
    return conn.execute(
        "SELECT status, close_status, close_reason, realized_pnl FROM structures "
        "WHERE structure_id='abc123'"
    ).fetchone()


def _expiry(symbol, activity_type="OPEXP"):
    return {"activity_type": activity_type, "symbol": symbol, "date": "2026-09-18"}


# ---- reconcile_pending_orders -------------------------------------------


@pytest.mark.parametrize("dead", ["canceled", "expired", "rejected"])
def test_dead_close_order_returns_structure_to_open(tmp_path, monkeypatch, dead):
    conn = _connect(tmp_path, monkeypatch)
    _open_structure(conn)
    record_close_submission(
        conn, "abc123", reason="close_by_dte",
        order={"id": "alpaca-close", "client_order_id": "opt-close", "status": "pending_new"},
    )
    reconcile_pending_orders(conn, FakeTrader({"alpaca-close": {"status": dead}}), NOW)
    assert _status(conn)[:2] == ("open", dead)
    # ... so the exit pass, which only acts on `open`, resubmits it.
    assert [s["status"] for s in fetch_open_structures(conn, EXPERIMENT)] == ["open"]


def test_working_close_order_stays_closing_pending(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    _open_structure(conn)
    record_close_submission(
        conn, "abc123", reason="close_by_dte",
        order={"id": "alpaca-close", "client_order_id": "opt-close", "status": "pending_new"},
    )
    reconcile_pending_orders(conn, FakeTrader({"alpaca-close": {"status": "new"}}), NOW)
    assert _status(conn)[0] == "closing_pending"


def test_dead_open_order_frees_the_one_structure_cap(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    insert_structure(
        conn, _approved(), _proposal(NOW), structure_id="abc123", ts=NOW.isoformat(),
        order={"id": "alpaca-open", "client_order_id": "opt-open", "status": "accepted"},
    )
    reconcile_pending_orders(conn, FakeTrader({"alpaca-open": {"status": "expired"}}), NOW)
    assert _status(conn)[0] == "open_failed"
    assert fetch_open_structures(conn, EXPERIMENT) == []


# ---- expiry settlement ---------------------------------------------------


def test_expired_worthless_needs_an_expiry_on_every_leg():
    structure = {"legs": [{"symbol": SHORT}, {"symbol": LONG}]}
    assert expired_worthless(structure, [_expiry(SHORT), _expiry(LONG)])
    assert not expired_worthless(structure, [_expiry(SHORT)])
    assert not expired_worthless(structure, [])
    # Another structure's expiries are not evidence about this one.
    assert not expired_worthless(structure, [_expiry("SPY260918P00700000"), _expiry(LONG)])


@pytest.mark.parametrize("activity_type", ["OPASN", "OPEXC"])
def test_assignment_or_exercise_is_never_settled_automatically(activity_type):
    structure = {"legs": [{"symbol": SHORT}, {"symbol": LONG}]}
    activities = [_expiry(SHORT, activity_type), _expiry(LONG)]
    assert not expired_worthless(structure, activities)


def test_settle_books_the_full_credit_from_the_open_fill(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    _open_structure(conn, open_fill_px=-0.60)
    trader = FakeTrader(activities=[_expiry(SHORT), _expiry(LONG)])
    settle_expired_structures(conn, trader, [], dt.date(2026, 9, 21), NOW)
    assert _status(conn) == ("closed", "expired", "expired_worthless", pytest.approx(60.0))
    assert fetch_open_structures(conn, EXPERIMENT) == []


def test_settle_falls_back_to_the_approved_credit(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    _open_structure(conn, open_fill_px=None)
    trader = FakeTrader(activities=[_expiry(SHORT), _expiry(LONG)])
    settle_expired_structures(conn, trader, [], dt.date(2026, 9, 21), NOW)
    assert _status(conn)[3] == pytest.approx(67.0)


@pytest.mark.parametrize("today, positions, activities", [
    # Not past expiration yet (expiration day itself is still live).
    (dt.date(2026, 9, 18), [], [_expiry(SHORT), _expiry(LONG)]),
    # A leg is still at the broker.
    (dt.date(2026, 9, 21), [{"symbol": LONG, "qty": "1"}], [_expiry(SHORT), _expiry(LONG)]),
    # The broker has reported nothing.
    (dt.date(2026, 9, 21), [], []),
    # The short leg was assigned.
    (dt.date(2026, 9, 21), [], [_expiry(SHORT, "OPASN"), _expiry(LONG)]),
])
def test_settle_leaves_anything_unproven_open(tmp_path, monkeypatch, today, positions, activities):
    conn = _connect(tmp_path, monkeypatch)
    _open_structure(conn)
    settle_expired_structures(conn, FakeTrader(activities=activities), positions, today, NOW)
    assert _status(conn)[0] == "open"


def test_the_f927d483_sequence_clears_in_one_run(tmp_path, monkeypatch):
    """closing_pending with a canceled close order, legs expired worthless:
    one pass of reconcile + settle leaves nothing open."""
    conn = _connect(tmp_path, monkeypatch)
    _open_structure(conn)
    record_close_submission(
        conn, "abc123", reason="close_by_dte",
        order={"id": "alpaca-close", "client_order_id": "opt-close", "status": "pending_new"},
    )
    trader = FakeTrader(
        {"alpaca-close": {"status": "canceled"}}, [_expiry(SHORT), _expiry(LONG)]
    )
    today = dt.date(2026, 9, 21)
    reconcile_pending_orders(conn, trader, NOW)
    settle_expired_structures(conn, trader, [], today, NOW)
    assert _status(conn)[0] == "closed"
    assert fetch_open_structures(conn, EXPERIMENT) == []


# ---- finished_at_broker (scripts/healthcheck.py's read-only filter) -------


def test_finished_at_broker_covers_a_filled_close_and_a_worthless_expiry(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    _open_structure(conn)
    structures = fetch_open_structures(conn, EXPERIMENT)
    expired = FakeTrader(activities=[_expiry(SHORT), _expiry(LONG)])
    assert finished_at_broker(expired, structures, [], dt.date(2026, 9, 21)) == {"abc123"}
    # Legs still held, or no broker evidence: still reconciled, still pages.
    held = [{"symbol": SHORT, "qty": "-1"}, {"symbol": LONG, "qty": "1"}]
    assert finished_at_broker(expired, structures, held, dt.date(2026, 9, 21)) == set()
    assert finished_at_broker(FakeTrader(), structures, [], dt.date(2026, 9, 21)) == set()

    record_close_submission(
        conn, "abc123", reason="close_by_dte",
        order={"id": "alpaca-close", "client_order_id": "opt-close", "status": "new"},
    )
    structures = fetch_open_structures(conn, EXPERIMENT)
    filled = FakeTrader({"alpaca-close": {"status": "filled"}})
    assert finished_at_broker(filled, structures, [], dt.date(2026, 9, 15)) == {"abc123"}
    working = FakeTrader({"alpaca-close": {"status": "new"}})
    assert finished_at_broker(working, structures, [], dt.date(2026, 9, 15)) == set()


# ---- closing_debit ---------------------------------------------------------


def _structure():
    return {"legs": [
        {"symbol": SHORT, "position_intent": "sell_to_open", "ratio_qty": 1},
        {"symbol": LONG, "position_intent": "buy_to_open", "ratio_qty": 1},
    ]}


def _quotes(short_bid, short_ask, long_bid, long_ask):
    return {
        SHORT: {"latestQuote": {"bp": short_bid, "ap": short_ask}},
        LONG: {"latestQuote": {"bp": long_bid, "ap": long_ask}},
    }


def test_occ_strike():
    assert occ_strike("SPY260918P00751000") == 751.0
    assert occ_strike("SPY260918P00744500") == 744.5


def test_closing_debit_pays_the_short_ask_and_takes_the_long_bid():
    # 1.40 - 0.33 = 1.07 at the touch, plus the one-cent floor allowance.
    assert closing_debit(_structure(), _quotes(1.35, 1.40, 0.33, 0.36)) == pytest.approx(1.08)


def test_closing_debit_follows_the_market_above_the_original_credit():
    # The 2026-09-14 case: credit 0.54, spread trading around 1.00 — a
    # limit pinned to the credit could never fill.
    assert closing_debit(_structure(), _quotes(1.00, 1.05, 0.04, 0.06)) > 0.54


def test_closing_debit_is_capped_at_the_strike_width():
    assert closing_debit(_structure(), _quotes(9.0, 9.5, 0.5, 0.6)) == 5.0


def test_closing_debit_never_drops_below_a_cent():
    assert closing_debit(_structure(), _quotes(0.01, 0.01, 0.02, 0.03)) == 0.01


@pytest.mark.parametrize("snapshots", [
    {},
    {SHORT: {"latestQuote": {"bp": 1.0, "ap": 1.1}}},
    _quotes(1.0, 0, 0.3, 0.4),
    _quotes(1.0, 1.1, 0, 0.4),
])
def test_closing_debit_is_none_without_both_quotes(snapshots):
    assert closing_debit(_structure(), snapshots) is None
