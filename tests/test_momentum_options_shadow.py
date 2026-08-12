import datetime as dt

import pytest

from scripts.momentum_options_shadow import parse_option, select_directional_vertical


def _snapshot(bid, ask, delta):
    return {
        "latestQuote": {"bp": bid, "ap": ask, "bs": 5, "as": 5, "t": "t"},
        "greeks": {"delta": delta},
    }


def test_bull_call_vertical_uses_ask_to_buy_and_bid_to_sell():
    snapshots = {
        "AAPL260918C00200000": _snapshot(5.0, 5.2, 0.60),
        "AAPL260918C00210000": _snapshot(2.0, 2.2, 0.35),
    }
    row = select_directional_vertical(
        snapshots, underlying="AAPL", direction="bull_call",
        today=dt.date(2026, 8, 12), target_dte=45,
        long_delta=0.60, short_delta=0.35,
    )
    assert row["net_debit"] == pytest.approx(3.2)
    assert row["maximum_loss"] == pytest.approx(320)
    assert row["maximum_profit"] == pytest.approx(680)
    assert row["reward_to_risk"] == pytest.approx(2.125)


def test_bear_put_vertical_orders_strikes_correctly():
    snapshots = {
        "AAPL260918P00200000": _snapshot(5.0, 5.2, -0.60),
        "AAPL260918P00190000": _snapshot(2.0, 2.2, -0.35),
    }
    row = select_directional_vertical(
        snapshots, underlying="AAPL", direction="bear_put",
        today=dt.date(2026, 8, 12), target_dte=45,
        long_delta=0.60, short_delta=0.35,
    )
    assert row["long_strike"] == 200
    assert row["short_strike"] == 190


def test_parse_option_preserves_root_kind_and_decimal_strike():
    assert parse_option("NVDA260918P00187500") == (
        "NVDA", dt.date(2026, 9, 18), "put", 187.5
    )
