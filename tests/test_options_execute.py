from __future__ import annotations

import pytest

from engine.data import AlpacaError
from engine.execute import OptionLeg, Trader, mirror_closing_legs


def trader_without_network() -> Trader:
    return object.__new__(Trader)


def _spread_legs():
    return [
        OptionLeg("SPY260918P00744000", "sell", "sell_to_open", 1),
        OptionLeg("SPY260918P00739000", "buy", "buy_to_open", 1),
    ]


def test_multi_leg_order_payload_shape(monkeypatch):
    trader = trader_without_network()
    captured = {}
    monkeypatch.setattr(
        trader, "_post", lambda path, payload: captured.update(path=path, payload=payload) or {"id": "opt-1"}
    )
    result = trader.submit_multi_leg_order(
        _spread_legs(), qty=2, credit=0.67, client_order_id="opt-open-1"
    )
    assert result["id"] == "opt-1"
    assert captured["path"] == "/v2/orders"
    payload = captured["payload"]
    assert payload["order_class"] == "mleg"
    assert payload["type"] == "limit"
    assert payload["time_in_force"] == "day"
    assert payload["qty"] == "2"
    assert payload["client_order_id"] == "opt-open-1"
    assert payload["legs"] == [
        {"symbol": "SPY260918P00744000", "side": "sell",
         "position_intent": "sell_to_open", "ratio_qty": "1"},
        {"symbol": "SPY260918P00739000", "side": "buy",
         "position_intent": "buy_to_open", "ratio_qty": "1"},
    ]


def test_credit_negated_and_floored_conservatively(monkeypatch):
    """Alpaca's limit_price convention is the opposite of options_shadow.py's
    credit convention (positive=debit, negative=credit) — a credit of 0.485
    must submit as -0.49 (demanding *at least* $0.49, never less than the
    gate-approved $0.485)."""
    trader = trader_without_network()
    captured = {}
    monkeypatch.setattr(trader, "_post", lambda path, payload: captured.update(payload=payload) or {})
    trader.submit_multi_leg_order(_spread_legs(), qty=1, credit=0.485)
    assert captured["payload"]["limit_price"] == "-0.49"


def test_debit_floored_toward_paying_less(monkeypatch):
    """A debit (negative credit) must never round toward paying MORE than
    what was approved."""
    trader = trader_without_network()
    captured = {}
    monkeypatch.setattr(trader, "_post", lambda path, payload: captured.update(payload=payload) or {})
    trader.submit_multi_leg_order(_spread_legs(), qty=1, credit=-0.505)
    assert captured["payload"]["limit_price"] == "0.5"


def test_rejects_too_few_legs():
    trader = trader_without_network()
    with pytest.raises(AlpacaError, match="2-4 legs"):
        trader.submit_multi_leg_order(_spread_legs()[:1], qty=1, credit=0.5)


def test_rejects_too_many_legs():
    trader = trader_without_network()
    legs = _spread_legs() + [
        OptionLeg("SPY260918P00734000", "sell", "sell_to_open", 1),
        OptionLeg("SPY260918P00729000", "buy", "buy_to_open", 1),
        OptionLeg("SPY260918P00724000", "buy", "buy_to_open", 1),
    ]
    with pytest.raises(AlpacaError, match="2-4 legs"):
        trader.submit_multi_leg_order(legs, qty=1, credit=0.5)


def test_rejects_mixed_opening_and_closing_intents():
    trader = trader_without_network()
    legs = [
        OptionLeg("SPY260918P00744000", "buy", "buy_to_close", 1),
        OptionLeg("SPY260918P00739000", "buy", "buy_to_open", 1),
    ]
    with pytest.raises(AlpacaError, match="uniformly opening or uniformly closing"):
        trader.submit_multi_leg_order(legs, qty=1, credit=0.5)


def test_rejects_non_positive_ratio_qty():
    trader = trader_without_network()
    legs = [
        OptionLeg("SPY260918P00744000", "sell", "sell_to_open", 0),
        OptionLeg("SPY260918P00739000", "buy", "buy_to_open", 1),
    ]
    with pytest.raises(AlpacaError, match="ratio_qty"):
        trader.submit_multi_leg_order(legs, qty=1, credit=0.5)


def test_rejects_qty_rounding_to_zero():
    trader = trader_without_network()
    with pytest.raises(AlpacaError, match="rounds to zero"):
        trader.submit_multi_leg_order(_spread_legs(), qty=0, credit=0.5)


def test_mirror_closing_legs_flips_intent_and_side():
    legs = _spread_legs()
    closing = mirror_closing_legs(legs)
    assert closing == [
        OptionLeg("SPY260918P00744000", "buy", "buy_to_close", 1),
        OptionLeg("SPY260918P00739000", "sell", "sell_to_close", 1),
    ]


def test_mirror_closing_legs_rejects_an_already_closing_leg():
    already_closing = [OptionLeg("SPY260918P00744000", "buy", "buy_to_close", 1)]
    with pytest.raises(AlpacaError, match="non-opening leg"):
        mirror_closing_legs(already_closing)


def test_multi_leg_order_client_order_id_truncated(monkeypatch):
    trader = trader_without_network()
    captured = {}
    monkeypatch.setattr(trader, "_post", lambda path, payload: captured.update(payload=payload) or {})
    long_id = "x" * 100
    trader.submit_multi_leg_order(_spread_legs(), qty=1, credit=0.5, client_order_id=long_id)
    assert len(captured["payload"]["client_order_id"]) == 48
