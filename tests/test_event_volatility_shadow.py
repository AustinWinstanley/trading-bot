import datetime as dt

import pytest

from scripts.event_volatility_shadow import select_atm_straddle


def _snap(bid, ask, delta):
    return {"latestQuote": {"bp": bid, "ap": ask, "bs": 2, "as": 2},
            "greeks": {"delta": delta}}


def test_atm_straddle_uses_first_expiry_after_event_and_both_asks():
    snapshots = {
        "SPY260918C00700000": _snap(10, 10.2, .51),
        "SPY260918P00700000": _snap(9.8, 10.0, -.49),
        "SPY260925C00700000": _snap(12, 12.2, .51),
        "SPY260925P00700000": _snap(11.8, 12.0, -.49),
    }
    row = select_atm_straddle(
        snapshots, spot=701, event_date=dt.date(2026, 9, 16)
    )
    assert row["expiration_date"] == "2026-09-18"
    assert row["strike"] == 700
    assert row["straddle_debit"] == pytest.approx(20.2)
    assert row["implied_break_even_move_pct"] == pytest.approx(20.2 / 701)
