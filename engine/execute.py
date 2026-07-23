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

from engine.data import AlpacaClient, AlpacaError


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
