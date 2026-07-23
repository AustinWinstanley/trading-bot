from __future__ import annotations

import pytest

from engine.data import AlpacaError
from engine.execute import Trader


def trader_without_network() -> Trader:
    trader = object.__new__(Trader)
    return trader


def test_protected_long_is_an_oto_with_stop(monkeypatch):
    trader = trader_without_network()
    captured = {}

    def fake_post(path, payload):
        captured.update(path=path, payload=payload)
        return {"id": "paper-order"}

    monkeypatch.setattr(trader, "_post", fake_post)
    result = trader.submit_protected_limit(
        "XLK", "buy", 1.23456789, 100.0, 92.0,
        client_order_id="entry-1",
    )

    assert result["id"] == "paper-order"
    assert captured["path"] == "/v2/orders"
    payload = captured["payload"]
    assert payload["order_class"] == "oto"
    assert payload["time_in_force"] == "day"
    assert payload["qty"] == "1.234567"
    assert payload["stop_loss"] == {"stop_price": "92.0"}


def test_protected_short_has_stop_above_entry(monkeypatch):
    trader = trader_without_network()
    payloads = []
    monkeypatch.setattr(
        trader, "_post",
        lambda path, payload: payloads.append(payload) or {"id": "short"},
    )
    trader.submit_protected_limit("XLK", "sell", 5.0, 100.0, 108.0)
    assert payloads[0]["stop_loss"]["stop_price"] == "108.0"


@pytest.mark.parametrize(
    "side,entry,stop",
    [
        ("buy", 100.0, 100.0),
        ("buy", 100.0, 101.0),
        ("sell", 100.0, 100.0),
        ("sell", 100.0, 99.0),
    ],
)
def test_protected_order_rejects_stop_on_wrong_side(side, entry, stop):
    trader = trader_without_network()
    with pytest.raises(AlpacaError):
        trader.submit_protected_limit("XLK", side, 1.0, entry, stop)
