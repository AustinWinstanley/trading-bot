import datetime as dt

import pytest

from scripts.options_shadow import (
    parse_contract,
    select_delta_quote_pair,
    select_put_broken_wing,
    select_quote_pair,
)


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


def test_delta_selection_uses_nearest_delta_with_exact_lower_wing():
    snapshots = {
        "SPY260918P00710000": _snapshot(2.0, 2.1, -0.24),
        "SPY260918P00705000": _snapshot(1.3, 1.4, -0.20),
        "SPY260918P00700000": _snapshot(0.8, 0.9, -0.16),
    }
    row = select_delta_quote_pair(
        snapshots,
        today=dt.date(2026, 8, 12),
        target_dte=45,
        target_delta=0.20,
        width=5.0,
    )
    assert row["short_strike"] == 705
    assert row["long_strike"] == 700
    assert row["short_delta"] == -0.20
    assert row["executable_credit"] == pytest.approx(0.4)


def test_broken_wing_prices_one_by_two_by_one_at_executable_quotes():
    snapshots = {
        "SPY260918P00710000": _snapshot(2.00, 2.10, -0.25),
        "SPY260918P00708000": _snapshot(1.60, 1.70, -0.22),
        "SPY260918P00704000": _snapshot(1.00, 1.10, -0.16),
    }
    row = select_put_broken_wing(
        snapshots,
        today=dt.date(2026, 8, 12),
        target_dte=45,
        target_upper_delta=0.25,
        upper_width=2.0,
        lower_width=4.0,
    )
    # Buy upper ask 2.10 + lower ask 1.10 - 2 * middle bid 1.60.
    assert row["net_debit"] == pytest.approx(0.0)
    assert row["peak_profit"] == pytest.approx(200)
    assert row["maximum_loss"] == pytest.approx(200)
    assert row["lower_long_strike"] == 704
