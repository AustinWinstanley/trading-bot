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
        "XLK", "buy", 2.0, 100.0, 92.0,
        client_order_id="entry-1",
    )

    assert result["id"] == "paper-order"
    assert captured["path"] == "/v2/orders"
    payload = captured["payload"]
    assert payload["order_class"] == "oto"
    assert payload["time_in_force"] == "day"
    assert payload["qty"] == "2"
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


def test_fractional_entry_is_simple_and_reports_software_stop(monkeypatch):
    trader = trader_without_network()
    payloads = []
    monkeypatch.setattr(
        trader,
        "_post",
        lambda path, payload: payloads.append(payload) or {"id": "fractional"},
    )
    order, broker_protected = trader.submit_entry(
        "XLK", "buy", 1.23456789, 100.0, 92.0,
        client_order_id="entry-1",
    )
    assert order["id"] == "fractional"
    assert not broker_protected
    assert payloads[0]["qty"] == "1.234567"
    assert "order_class" not in payloads[0]


def test_whole_share_entry_uses_broker_oto(monkeypatch):
    trader = trader_without_network()
    payloads = []
    monkeypatch.setattr(
        trader,
        "_post",
        lambda path, payload: payloads.append(payload) or {"id": "whole"},
    )
    _, broker_protected = trader.submit_entry(
        "XLK", "buy", 2.0, 100.0, 92.0,
    )
    assert broker_protected
    assert payloads[0]["order_class"] == "oto"


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


def test_submit_entry_sends_a_plain_limit_when_there_is_no_stop(monkeypatch):
    """A zero stop means the sleeve is deliberately unstopped. Sending it as an
    OTO would attach a stop at 0 -- inert for a long, instant for a short."""
    from engine.execute import Trader

    t = Trader.__new__(Trader)
    calls = []
    monkeypatch.setattr(
        Trader, "submit_limit",
        lambda self, *a, **k: calls.append(("limit", a, k)) or {"id": "x"},
    )
    monkeypatch.setattr(
        Trader, "submit_protected_limit",
        lambda self, *a, **k: calls.append(("oto", a, k)) or {"id": "y"},
    )

    order, broker_protected = t.submit_entry("MU", "sell", 5.0, 100.0, 0.0)
    assert [c[0] for c in calls] == ["limit"]
    assert broker_protected is False

    calls.clear()
    t.submit_entry("MU", "sell", 5.0, 100.0, 110.0)
    assert [c[0] for c in calls] == ["oto"]
