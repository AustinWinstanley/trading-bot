"""Tests for scripts/options_daily.py's reconcile_option_structures — the
pure, unit-testable assignment-detection backstop. No automatic
remediation is tested here because none exists: this function only ever
returns a list of findings for the caller to CRITICAL and page on.
"""

from __future__ import annotations

import sqlite3

from scripts.options_daily import (
    equity_qty_explained_by_orders,
    load_equity_explained_qty,
    reconcile_option_structures,
)


def _structure(structure_id="abc123", underlying="SPY"):
    return {
        "structure_id": structure_id,
        "underlying": underlying,
        "legs": [
            {"symbol": "SPY260918P00744000", "position_intent": "sell_to_open"},
            {"symbol": "SPY260918P00739000", "position_intent": "buy_to_open"},
        ],
    }


def _position(symbol, qty, asset_class="us_option"):
    return {"symbol": symbol, "qty": str(qty), "asset_class": asset_class}


def test_clean_state_has_no_findings():
    positions = [
        _position("SPY260918P00744000", -1),  # short leg
        _position("SPY260918P00739000", 1),   # long leg
    ]
    findings = reconcile_option_structures(positions, [_structure()])
    assert findings == []


def test_no_open_structures_and_no_positions_is_clean():
    assert reconcile_option_structures([], []) == []


def test_missing_leg_is_flagged():
    positions = [_position("SPY260918P00739000", 1)]  # short leg missing entirely
    findings = reconcile_option_structures(positions, [_structure()])
    assert len(findings) == 1
    assert "SPY260918P00744000" in findings[0]
    assert "missing" in findings[0]


def test_wrong_sign_on_a_leg_is_flagged():
    positions = [
        _position("SPY260918P00744000", 1),   # should be short (negative), is long
        _position("SPY260918P00739000", 1),
    ]
    findings = reconcile_option_structures(positions, [_structure()])
    assert len(findings) == 1
    assert "SPY260918P00744000" in findings[0]
    assert "unexpected sign" in findings[0]


def test_zero_qty_leg_is_flagged_as_missing():
    positions = [
        _position("SPY260918P00744000", 0),
        _position("SPY260918P00739000", 1),
    ]
    findings = reconcile_option_structures(positions, [_structure()])
    assert len(findings) == 1
    assert "missing" in findings[0]


def test_unexplained_equity_position_in_underlying_is_flagged():
    """With no equity_explained_qty passed, the whole broker quantity is
    treated as unexplained — the original, stricter behavior, still
    correct for an underlying this system has no equity order history in
    at all."""
    positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "SPY", "qty": "100", "asset_class": "us_equity"},
    ]
    findings = reconcile_option_structures(positions, [_structure()])
    assert len(findings) == 1
    assert "possible option assignment" in findings[0]


def test_equity_position_fully_explained_by_own_orders_is_not_flagged():
    """The false positive this whole mechanism exists to fix: SPY is both
    the options experiment's only underlying and equity_core/trend's core
    holding, so an unqualified check flags on every single day the
    structure is open. equity_explained_qty is how the caller says "this
    much of the broker quantity is accounted for by our own equity
    orders." """
    positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "SPY", "qty": "15.649385", "asset_class": "us_equity"},
    ]
    findings = reconcile_option_structures(
        positions, [_structure()], equity_explained_qty={"SPY": 15.649385}
    )
    assert findings == []


def test_small_explained_delta_is_not_flagged():
    """Fractional-share rounding noise (a few thousandths of a share) from
    ordinary rebalancing must not trip this — only a real assignment-sized
    move (whole contracts, i.e. 100-share lots) should."""
    positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "SPY", "qty": "15.652", "asset_class": "us_equity"},
    ]
    findings = reconcile_option_structures(
        positions, [_structure()], equity_explained_qty={"SPY": 15.649385}
    )
    assert findings == []


def test_unexplained_delta_on_top_of_an_explained_position_is_flagged():
    """The realistic assignment scenario for this account: SPY already has
    a legitimate equity_core/trend holding (explained), and assignment on
    the short leg of the spread would ADD a full contract's worth of
    shares (100) on top of it — the sum is still "an equity position in
    SPY", but the DELTA beyond what orders explain is the real signal."""
    positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "SPY", "qty": "115.649385", "asset_class": "us_equity"},
    ]
    findings = reconcile_option_structures(
        positions, [_structure()], equity_explained_qty={"SPY": 15.649385}
    )
    assert len(findings) == 1
    assert "possible option assignment" in findings[0]
    assert "+100.0000" in findings[0]


def test_equity_position_in_an_unrelated_symbol_is_not_flagged():
    positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "QQQ", "qty": "50", "asset_class": "us_equity"},
    ]
    findings = reconcile_option_structures(positions, [_structure()])
    assert findings == []


def test_equity_position_with_no_open_structures_is_not_flagged():
    """Only flagged when it coincides with an open structure in that
    underlying — an equity position with no options activity at all is not
    this function's concern."""
    positions = [{"symbol": "SPY", "qty": "100", "asset_class": "us_equity"}]
    findings = reconcile_option_structures(positions, [])
    assert findings == []


def test_multiple_findings_all_reported():
    positions = [
        {"symbol": "SPY", "qty": "100", "asset_class": "us_equity"},
    ]  # both legs missing AND an unexplained equity position
    findings = reconcile_option_structures(positions, [_structure()])
    assert len(findings) == 3


def _equity_journal(tmp_path, symbol_orders):
    """Build a real, minimal equity journal (state/paper*.db's own orders
    schema) so equity_qty_explained_by_orders/load_equity_explained_qty
    are tested against real SQL, not a mock. symbol_orders is a list of
    (symbol, side, filled_qty, status) tuples."""
    db_path = tmp_path / "paper_2x.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE orders(ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, "
        "qty REAL, notional REAL, limit_price REAL, stop_price REAL, "
        "reason TEXT, alpaca_id TEXT, status TEXT, requested_notional REAL, "
        "reference_price REAL, filled_qty REAL, filled_avg_price REAL, "
        "filled_at TEXT)"
    )
    for symbol, side, filled_qty, status in symbol_orders:
        conn.execute(
            "INSERT INTO orders(ts, symbol, side, sleeve, filled_qty, status) "
            "VALUES ('2026-08-01T00:00:00', ?, ?, 'equity_core+trend', ?, ?)",
            (symbol, side, filled_qty, status),
        )
    conn.commit()
    conn.close()
    return db_path


def test_equity_qty_explained_by_orders_nets_buys_and_sells(tmp_path):
    db_path = _equity_journal(tmp_path, [
        ("SPY", "buy", 10.0, "filled"),
        ("SPY", "buy", 6.0, "filled"),
        ("SPY", "sell", 1.0, "filled"),
    ])
    conn = sqlite3.connect(db_path)
    assert equity_qty_explained_by_orders(conn, "SPY") == 15.0


def test_equity_qty_explained_by_orders_nets_shorts_and_covers(tmp_path):
    db_path = _equity_journal(tmp_path, [
        ("Z", "short", 5.0, "filled"),
        ("Z", "cover", 2.0, "filled"),
    ])
    conn = sqlite3.connect(db_path)
    # short adds to the negative side; cover reduces it back toward zero —
    # net position implied by orders is -3 shares.
    assert equity_qty_explained_by_orders(conn, "Z") == -3.0


def test_equity_qty_explained_by_orders_ignores_unfilled_and_other_symbols(tmp_path):
    db_path = _equity_journal(tmp_path, [
        ("SPY", "buy", 10.0, "filled"),
        ("SPY", "buy", 999.0, "expired"),  # never filled — must not count
        ("QQQ", "buy", 50.0, "filled"),    # different symbol — must not count
    ])
    conn = sqlite3.connect(db_path)
    assert equity_qty_explained_by_orders(conn, "SPY") == 10.0


def test_equity_qty_explained_by_orders_unknown_symbol_is_zero(tmp_path):
    db_path = _equity_journal(tmp_path, [("SPY", "buy", 10.0, "filled")])
    conn = sqlite3.connect(db_path)
    assert equity_qty_explained_by_orders(conn, "QQQ") == 0.0


def test_load_equity_explained_qty_missing_file_returns_empty(tmp_path):
    missing = tmp_path / "paper_2x.db"
    assert load_equity_explained_qty(missing, [_structure()]) == {}
    assert not missing.exists()  # must stay read-only, never create the file


def test_load_equity_explained_qty_schemaless_file_returns_empty(tmp_path):
    schemaless = tmp_path / "schemaless.db"
    schemaless.touch()
    assert load_equity_explained_qty(schemaless, [_structure()]) == {}


def test_load_equity_explained_qty_no_open_structures_returns_empty(tmp_path):
    db_path = _equity_journal(tmp_path, [("SPY", "buy", 10.0, "filled")])
    assert load_equity_explained_qty(db_path, []) == {}


def test_load_equity_explained_qty_computes_real_map(tmp_path):
    db_path = _equity_journal(tmp_path, [
        ("SPY", "buy", 11.604043, "filled"),
        ("SPY", "buy", 4.045342, "filled"),
    ])
    result = load_equity_explained_qty(db_path, [_structure()])
    assert result == {"SPY": 11.604043 + 4.045342}


def test_end_to_end_real_journal_explains_position_and_flags_assignment_delta(tmp_path):
    """The full path this fix exists for: a real equity journal explains
    the ordinary equity_core/trend SPY holding, so the options
    reconciliation only flags the genuinely unexplained delta."""
    db_path = _equity_journal(tmp_path, [
        ("SPY", "buy", 11.604043, "filled"),
        ("SPY", "buy", 4.045342, "filled"),
    ])
    structures = [_structure()]
    equity_explained_qty = load_equity_explained_qty(db_path, structures)

    clean_positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "SPY", "qty": "15.649385", "asset_class": "us_equity"},
    ]
    assert reconcile_option_structures(
        clean_positions, structures, equity_explained_qty=equity_explained_qty
    ) == []

    assigned_positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "SPY", "qty": "115.649385", "asset_class": "us_equity"},
    ]
    findings = reconcile_option_structures(
        assigned_positions, structures, equity_explained_qty=equity_explained_qty
    )
    assert len(findings) == 1
    assert "possible option assignment" in findings[0]
