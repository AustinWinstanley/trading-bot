import datetime as dt

import pytest

from scripts.options_shadow import parse_contract, select_quote_pair


def _snapshot(bid, ask, delta):
    return {
        "latestQuote": {"bp": bid, "ap": ask, "t": "t"},
        "greeks": {"delta": delta},
        "impliedVolatility": 0.2,
    }


def test_select_quote_pair_uses_conservative_executable_credit():
    snapshots = {
        "SPY260918P00695000": _snapshot(1.20, 1.25, -0.10),
        "SPY260918P00690000": _snapshot(0.55, 0.60, -0.07),
    }
    row = select_quote_pair(
        snapshots,
        spot=772.0,
        today=dt.date(2026, 8, 12),
        target_dte=45,
        short_moneyness=0.90,
        width=5.0,
    )
    assert row["short_strike"] == 695
    assert row["long_strike"] == 690
    assert row["executable_credit"] == 0.60
    assert row["maximum_loss"] == pytest.approx(440)


def test_parse_contract_rejects_non_spy_put():
    assert parse_contract("SPY260918P00695000") == (dt.date(2026, 9, 18), 695.0)
    assert parse_contract("SPY260918C00695000") is None
