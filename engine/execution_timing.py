"""Matched-fill instrumentation for the base versus 2x timing experiment.

This module is read-only.  It pairs same-session, same-symbol, same-side
MOM_LS fills from the two paper journals and compares adverse slippage against
the reference price captured before each order.  It never changes cron or an
account; crossing the control sample threshold only produces a recommendation
for a separately reviewed schedule change.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = {
    "ts", "symbol", "side", "sleeve", "filled_qty", "reference_price",
    "filled_avg_price",
}


@dataclass(frozen=True)
class Fill:
    ts: dt.datetime
    symbol: str
    side: str
    filled_qty: float
    reference_price: float
    filled_avg_price: float

    @property
    def session(self) -> str:
        return self.ts.astimezone(ET).date().isoformat()

    @property
    def adverse_slippage_bps(self) -> float:
        direction = 1.0 if self.side in {"buy", "cover"} else -1.0
        return direction * (
            self.filled_avg_price - self.reference_price
        ) / self.reference_price * 10_000

    @property
    def reference_notional(self) -> float:
        return self.filled_qty * self.reference_price


def _parse_ts(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed


def load_fills(conn: sqlite3.Connection, since: str) -> list[Fill]:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(orders)")
    }
    if not REQUIRED_COLUMNS.issubset(columns):
        return []
    rows = conn.execute(
        "SELECT ts, symbol, side, filled_qty, reference_price, "
        "filled_avg_price FROM orders WHERE sleeve='mom_ls' AND ts>=? "
        "AND filled_qty>0 AND reference_price>0 AND filled_avg_price>0 "
        "ORDER BY ts",
        (since,),
    ).fetchall()
    return [
        Fill(
            ts=_parse_ts(str(row[0])),
            symbol=str(row[1]),
            side=str(row[2]),
            filled_qty=float(row[3]),
            reference_price=float(row[4]),
            filled_avg_price=float(row[5]),
        )
        for row in rows
    ]


def match_fills(
    base: list[Fill],
    leveraged: list[Fill],
    *,
    max_delta_minutes: float = 15.0,
) -> list[tuple[Fill, Fill]]:
    """Greedily pair nearest eligible fills without reusing an order."""
    by_key: dict[tuple[str, str, str], list[Fill]] = {}
    for fill in leveraged:
        by_key.setdefault((fill.session, fill.symbol, fill.side), []).append(fill)

    pairs = []
    used: set[tuple[tuple[str, str, str], int]] = set()
    max_seconds = max_delta_minutes * 60
    for left in base:
        key = (left.session, left.symbol, left.side)
        candidates = by_key.get(key, [])
        ranked = sorted(
            enumerate(candidates),
            key=lambda item: abs((item[1].ts - left.ts).total_seconds()),
        )
        for index, right in ranked:
            marker = (key, index)
            delta = abs((right.ts - left.ts).total_seconds())
            if marker not in used and delta <= max_seconds:
                used.add(marker)
                pairs.append((left, right))
                break
    return pairs


def _weighted(values: list[tuple[float, float]]) -> float | None:
    total = sum(weight for _, weight in values)
    if total <= 0:
        return None
    return sum(value * weight for value, weight in values) / total


def timing_summary(
    base_conn: sqlite3.Connection,
    leveraged_conn: sqlite3.Connection,
    *,
    since: str = "2026-08-04",
    max_delta_minutes: float = 15.0,
    control_min_pairs: int = 100,
) -> dict:
    base = load_fills(base_conn, since)
    leveraged = load_fills(leveraged_conn, since)
    pairs = match_fills(base, leveraged, max_delta_minutes=max_delta_minutes)
    weighted_base = []
    weighted_leveraged = []
    weighted_difference = []
    minute_deltas = []
    for left, right in pairs:
        weight = min(left.reference_notional, right.reference_notional)
        weighted_base.append((left.adverse_slippage_bps, weight))
        weighted_leveraged.append((right.adverse_slippage_bps, weight))
        weighted_difference.append((
            right.adverse_slippage_bps - left.adverse_slippage_bps,
            weight,
        ))
        minute_deltas.append((right.ts - left.ts).total_seconds() / 60)

    matched = len(pairs)
    return {
        "phase": "control",
        "since": since,
        "matched_fills": matched,
        "matched_sessions": len({left.session for left, _ in pairs}),
        "control_min_pairs": control_min_pairs,
        "control_complete": matched >= control_min_pairs,
        "base_adverse_slippage_bps": _weighted(weighted_base),
        "leveraged_adverse_slippage_bps": _weighted(weighted_leveraged),
        "leveraged_minus_base_bps": _weighted(weighted_difference),
        "average_schedule_delta_minutes": (
            sum(minute_deltas) / len(minute_deltas) if minute_deltas else None
        ),
        "recommendation": (
            "control sample complete; review a separate cron change for the "
            "fixed 20-session candidate phase"
            if matched >= control_min_pairs else
            f"continue the current schedule until {control_min_pairs} matched fills"
        ),
        "candidate_promotion_bar": {
            "minimum_candidate_sessions": 20,
            "minimum_2x_improvement_bps": 3.0,
            "maximum_base_degradation_bps": 1.0,
            "submission_or_lock_failures_may_not_increase": True,
        },
    }
