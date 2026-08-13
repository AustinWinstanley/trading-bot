"""Alpaca execution layer.

Deterministic Python only — no LLM anywhere in this module or below it.
Everything that reaches here has already passed engine/risk.py.

Orders are marketable DAY limit orders (config forbids market orders).
Fractional orders on Alpaca must be DAY time-in-force; qty is rounded DOWN to
6dp so rounding can never enlarge an order past what the gate approved.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Sequence

from engine.data import AlpacaClient, AlpacaError

# Alpaca-imposed limit on multi-leg option orders.
MAX_OPTION_LEGS = 4
_OPENING_INTENTS = frozenset({"buy_to_open", "sell_to_open"})
_CLOSING_INTENTS = frozenset({"buy_to_close", "sell_to_close"})
# opening intent -> (closing intent, closing side), the exact reversal
# needed to flatten a leg opened with that intent.
_CLOSING_MIRROR = {
    "buy_to_open": ("sell_to_close", "sell"),
    "sell_to_open": ("buy_to_close", "buy"),
}


@dataclass(frozen=True)
class OptionLeg:
    """One leg of a multi-leg options order.

    ``symbol`` is the OCC contract symbol. ``position_intent`` is Alpaca's
    own vocabulary (buy_to_open/sell_to_open/buy_to_close/sell_to_close) —
    ``side`` is the plain buy/sell Alpaca also requires alongside it.
    """

    symbol: str
    side: str
    position_intent: str
    ratio_qty: int = 1


def mirror_closing_legs(legs: Sequence[OptionLeg]) -> list[OptionLeg]:
    """The exact opposite legs that flatten an opened structure.

    buy_to_open -> sell_to_close (side buy -> sell); sell_to_open ->
    buy_to_close (side sell -> buy). Every leg passed in must be an opening
    leg — mirroring an already-closing leg is a caller bug, not a case to
    silently handle.
    """
    out = []
    for leg in legs:
        if leg.position_intent not in _CLOSING_MIRROR:
            raise AlpacaError(
                f"{leg.symbol}: cannot mirror a non-opening leg "
                f"(position_intent={leg.position_intent!r})"
            )
        intent, side = _CLOSING_MIRROR[leg.position_intent]
        out.append(OptionLeg(leg.symbol, side, intent, leg.ratio_qty))
    return out


class Trader(AlpacaClient):
    """Trading calls on top of the data client. Paper by default."""

    def _post(self, path: str, payload: dict) -> dict:
        self._limiter.acquire()
        r = self._session.post(f"{self.trading_base}{path}", json=payload, timeout=30)
        if r.status_code >= 400:
            raise AlpacaError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _delete(self, path: str) -> None:
        self._limiter.acquire()
        r = self._session.delete(f"{self.trading_base}{path}", timeout=30)
        if r.status_code >= 400 and r.status_code != 404:
            raise AlpacaError(f"DELETE {path} -> {r.status_code}: {r.text[:300]}")

    # -- market state ------------------------------------------------------

    def clock(self) -> dict:
        return self._get(self.trading_base, "/v2/clock")

    def latest_price(self, symbol: str) -> float | None:
        try:
            q = self._get(self.data_base, "/v2/stocks/trades/latest",
                          {"symbols": symbol, "feed": self.feed})
            t = (q.get("trades") or {}).get(symbol)
            return float(t["p"]) if t else None
        except (AlpacaError, KeyError, TypeError, ValueError):
            return None

    # -- orders ------------------------------------------------------------

    def open_orders(self) -> list[dict]:
        return self._get(self.trading_base, "/v2/orders", {"status": "open", "limit": 500})  # type: ignore

    def get_order(self, order_id: str) -> dict:
        return self._get(self.trading_base, f"/v2/orders/{order_id}")

    def cancel_order(self, order_id: str) -> None:
        self._delete(f"/v2/orders/{order_id}")

    def cancel_all_orders(self) -> None:
        self._delete("/v2/orders")

    def submit_limit(self, symbol: str, side: str, qty: float, limit_price: float,
                     *, client_order_id: str | None = None) -> dict:
        assert side in ("buy", "sell")
        qty = math.floor(qty * 1e6) / 1e6          # never round UP
        if qty <= 0:
            raise AlpacaError(f"{symbol}: qty rounds to zero")
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "qty": str(qty),
            "limit_price": str(round(limit_price, 2)),
            "time_in_force": "day",                # required for fractional
        }
        if client_order_id:
            payload["client_order_id"] = client_order_id[:48]
        return self._post("/v2/orders", payload)

    def submit_protected_limit(
        self,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        stop_price: float,
        *,
        client_order_id: str | None = None,
    ) -> dict:
        """Submit an entry with a broker-held stop activated after its fill.

        Alpaca's OTO order keeps the stop dormant until the parent entry fills,
        eliminating both the unprotected interval and phantom local stops for
        entries that never fill. Alpaca advanced orders require whole shares.
        """
        assert side in ("buy", "sell")
        qty = math.floor(qty)
        if qty < 1:
            raise AlpacaError(f"{symbol}: qty rounds to zero")
        if stop_price <= 0:
            raise AlpacaError(f"{symbol}: stop price must be positive")
        if side == "buy" and stop_price >= limit_price:
            raise AlpacaError(f"{symbol}: long stop must be below entry")
        if side == "sell" and stop_price <= limit_price:
            raise AlpacaError(f"{symbol}: short stop must be above entry")
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "qty": str(qty),
            "limit_price": str(round(limit_price, 2)),
            "time_in_force": "day",
            "order_class": "oto",
            "stop_loss": {"stop_price": str(round(stop_price, 2))},
        }
        if client_order_id:
            payload["client_order_id"] = client_order_id[:48]
        return self._post("/v2/orders", payload)

    def submit_entry(
        self,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        stop_price: float,
        *,
        client_order_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Submit an entry and report whether its stop is broker-held.

        Alpaca rejects fractional advanced orders. Whole-share entries use OTO;
        fractional entries use a simple DAY limit and must receive a software
        fallback stop from the runner.

        A falsy ``stop_price`` means the sleeve is deliberately unstopped
        (``risk.stop_exempt_sleeves``). Sending it as an OTO would attach a
        stop at zero — never triggering for a long, and triggering instantly
        for a short — so it goes out as a plain limit with no stop to mirror.
        """
        if not stop_price:
            return (
                self.submit_limit(
                    symbol, side, qty, limit_price, client_order_id=client_order_id
                ),
                False,
            )
        whole_qty = round(qty)
        if math.isclose(qty, whole_qty, abs_tol=1e-6):
            return (
                self.submit_protected_limit(
                    symbol,
                    side,
                    float(whole_qty),
                    limit_price,
                    stop_price,
                    client_order_id=client_order_id,
                ),
                True,
            )
        return (
            self.submit_limit(
                symbol,
                side,
                qty,
                limit_price,
                client_order_id=client_order_id,
            ),
            False,
        )

    def submit_multi_leg_order(
        self,
        legs: Sequence[OptionLeg],
        qty: int,
        credit: float,
        *,
        client_order_id: str | None = None,
    ) -> dict:
        """Submit a multi-leg (``order_class: "mleg"``) options order.

        ``credit`` uses scripts/options_shadow.py's sign convention:
        positive = net credit received, negative = net debit paid. Alpaca's
        own convention for ``limit_price`` is the OPPOSITE (positive =
        debit, negative = credit) — negated here so every call site can
        keep using the one convention the existing quote selectors already
        return.

        Rounds toward whichever cent value is strictly more conservative
        than what was computed — never demand less credit, or offer to pay
        more debit, than the value the risk gate actually approved. Never a
        market order, for the same reason no order anywhere in this repo
        is: config.execution.order_type forbids it for equities, and a
        multi-leg options order needs a bounded price even more, since an
        unbounded fill could exceed the structure's approved maximum loss.
        """
        if not (2 <= len(legs) <= MAX_OPTION_LEGS):
            raise AlpacaError(f"mleg order must have 2-{MAX_OPTION_LEGS} legs, got {len(legs)}")
        intents = {leg.position_intent for leg in legs}
        if not (intents <= _OPENING_INTENTS or intents <= _CLOSING_INTENTS):
            raise AlpacaError(
                f"mleg legs must be uniformly opening or uniformly closing, got {intents}"
            )
        for leg in legs:
            if leg.side not in ("buy", "sell"):
                raise AlpacaError(f"{leg.symbol}: side must be buy/sell, got {leg.side!r}")
            if not (isinstance(leg.ratio_qty, int) and leg.ratio_qty > 0):
                raise AlpacaError(
                    f"{leg.symbol}: ratio_qty must be a positive int, got {leg.ratio_qty!r}"
                )
        qty = math.floor(qty)
        if qty < 1:
            raise AlpacaError("mleg order: qty rounds to zero")
        limit_price = math.floor(-credit * 100) / 100
        payload = {
            "type": "limit",
            "time_in_force": "day",
            "order_class": "mleg",
            "qty": str(qty),
            "limit_price": str(round(limit_price, 2)),
            "legs": [
                {
                    "symbol": leg.symbol,
                    "side": leg.side,
                    "position_intent": leg.position_intent,
                    "ratio_qty": str(leg.ratio_qty),
                }
                for leg in legs
            ],
        }
        if client_order_id:
            payload["client_order_id"] = client_order_id[:48]
        return self._post("/v2/orders", payload)
