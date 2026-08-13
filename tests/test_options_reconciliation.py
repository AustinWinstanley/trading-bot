"""Tests for scripts/options_daily.py's reconcile_option_structures — the
pure, unit-testable assignment-detection backstop. No automatic
remediation is tested here because none exists: this function only ever
returns a list of findings for the caller to CRITICAL and page on.
"""

from __future__ import annotations

from scripts.options_daily import reconcile_option_structures


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
    positions = [
        _position("SPY260918P00744000", -1),
        _position("SPY260918P00739000", 1),
        {"symbol": "SPY", "qty": "100", "asset_class": "us_equity"},
    ]
    findings = reconcile_option_structures(positions, [_structure()])
    assert len(findings) == 1
    assert "possible option assignment" in findings[0]


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
