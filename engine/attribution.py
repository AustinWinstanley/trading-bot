"""Pure helpers for paper execution and exposure attribution."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _exposure(values: dict[str, float]) -> dict[str, float]:
    long = sum(value for value in values.values() if value > 0)
    short = sum(-value for value in values.values() if value < 0)
    return {
        "long": round(long, 6),
        "short": round(short, 6),
        "net": round(long - short, 6),
        "gross": round(long + short, 6),
    }


def build_exposure_attribution(
    *,
    equity: float,
    targets: dict[str, float],
    sleeve_targets: dict[str, dict[str, float]],
    positions: dict[str, Any],
) -> dict:
    """Compare target weights with broker-held weights by sleeve.

    Shared symbols (currently SPY) are allocated across sleeves in proportion
    to their absolute target contribution. Positions with no current target
    are assigned to ``unattributed`` so stale holdings remain visible.
    """
    if equity <= 0:
        raise ValueError("equity must be positive")

    actual = {
        symbol: float(position.market_value) / equity
        for symbol, position in positions.items()
    }
    target_by_sleeve = {
        sleeve: _exposure(weights)
        for sleeve, weights in sleeve_targets.items()
    }
    actual_weights: dict[str, dict[str, float]] = {
        sleeve: {} for sleeve in sleeve_targets
    }
    pnl_by_sleeve = {sleeve: 0.0 for sleeve in sleeve_targets}
    actual_weights["unattributed"] = {}
    pnl_by_sleeve["unattributed"] = 0.0

    for symbol, weight in actual.items():
        contributions = {
            sleeve: float(weights.get(symbol, 0.0))
            for sleeve, weights in sleeve_targets.items()
            if float(weights.get(symbol, 0.0)) != 0.0
        }
        total = sum(abs(value) for value in contributions.values())
        allocations = (
            {
                sleeve: abs(value) / total
                for sleeve, value in contributions.items()
            }
            if total
            else {"unattributed": 1.0}
        )
        position = positions[symbol]
        unrealized = float(position.qty) * (
            float(position.current_price) - float(position.avg_entry_price)
        )
        for sleeve, fraction in allocations.items():
            actual_weights.setdefault(sleeve, {})[symbol] = weight * fraction
            pnl_by_sleeve[sleeve] = (
                pnl_by_sleeve.get(sleeve, 0.0) + unrealized * fraction
            )

    actual_by_sleeve = {}
    for sleeve, weights in actual_weights.items():
        if not weights and sleeve == "unattributed":
            continue
        actual_by_sleeve[sleeve] = {
            **_exposure(weights),
            "unrealized_pl": round(pnl_by_sleeve.get(sleeve, 0.0), 2),
        }

    gaps = {
        symbol: round(actual.get(symbol, 0.0) - targets.get(symbol, 0.0), 6)
        for symbol in set(actual) | set(targets)
    }
    largest_gaps = [
        {"symbol": symbol, "weight_gap": gap}
        for symbol, gap in sorted(
            gaps.items(), key=lambda item: abs(item[1]), reverse=True
        )[:10]
    ]
    return {
        "target": _exposure(targets),
        "actual": _exposure(actual),
        "target_by_sleeve": target_by_sleeve,
        "actual_by_sleeve": actual_by_sleeve,
        "targets": {key: round(float(value), 6) for key, value in targets.items()},
        "actual_weights": {
            key: round(float(value), 6) for key, value in actual.items()
        },
        "largest_symbol_gaps": largest_gaps,
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }


def execution_summary(
    conn: sqlite3.Connection,
    since: str,
) -> dict:
    """Summarize fills, adverse slippage, shrinkage, and exposure."""
    order_columns = _table_columns(conn, "orders")
    required = {
        "ts", "sleeve", "qty", "notional", "status", "requested_notional",
        "reference_price", "filled_qty", "filled_avg_price",
    }
    orders: list[sqlite3.Row] = []
    if required.issubset(order_columns):
        conn.row_factory = sqlite3.Row
        orders = conn.execute(
            "SELECT ts, symbol, side, sleeve, qty, notional, status, "
            "requested_notional, reference_price, filled_qty, filled_avg_price "
            "FROM orders WHERE ts > ? ORDER BY ts",
            (since,),
        ).fetchall()

    def summarize(rows: list[sqlite3.Row]) -> dict:
        approved = sum(float(row["notional"] or 0) for row in rows)
        requested = sum(
            float(row["requested_notional"] or row["notional"] or 0)
            for row in rows
        )
        filled_reference = 0.0
        slippage_numerator = 0.0
        slippage_weight = 0.0
        filled_orders = 0
        for row in rows:
            qty = float(row["qty"] or 0)
            filled_qty = float(row["filled_qty"] or 0)
            reference = float(row["reference_price"] or 0)
            fill = float(row["filled_avg_price"] or 0)
            if filled_qty > 0:
                filled_orders += 1
            if qty > 0 and reference > 0:
                filled_reference += min(filled_qty / qty, 1.0) * float(
                    row["notional"] or 0
                )
            if filled_qty > 0 and reference > 0 and fill > 0:
                direction = 1.0 if row["side"] in ("buy", "cover") else -1.0
                adverse_bps = direction * (fill - reference) / reference * 10_000
                weight = filled_qty * reference
                slippage_numerator += adverse_bps * weight
                slippage_weight += weight
        statuses: dict[str, int] = {}
        for row in rows:
            status = str(row["status"] or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "orders": len(rows),
            "filled_orders": filled_orders,
            "requested_notional": round(requested, 2),
            "approved_notional": round(approved, 2),
            "approval_pct": round(approved / requested * 100, 1)
            if requested else 0.0,
            "fill_pct": round(filled_reference / approved * 100, 1)
            if approved else 0.0,
            "adverse_slippage_bps": round(
                slippage_numerator / slippage_weight, 2
            ) if slippage_weight else None,
            "statuses": statuses,
        }

    by_sleeve = {}
    for sleeve in sorted({str(row["sleeve"]) for row in orders}):
        by_sleeve[sleeve] = summarize(
            [row for row in orders if str(row["sleeve"]) == sleeve]
        )

    rejection_columns = _table_columns(conn, "rejections")
    rejection_rows = []
    if {"ts", "reason"}.issubset(rejection_columns):
        select = "reason"
        if "requested_notional" in rejection_columns:
            select += ", requested_notional"
        else:
            select += ", NULL AS requested_notional"
        conn.row_factory = sqlite3.Row
        rejection_rows = conn.execute(
            f"SELECT {select} FROM rejections WHERE ts > ?", (since,)
        ).fetchall()

    exposure = None
    if _table_columns(conn, "attribution_snapshots"):
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM attribution_snapshots WHERE ts > ? "
            "ORDER BY ts DESC LIMIT 1",
            (since,),
        ).fetchone()
        if row:
            exposure = {
                "ts": row["ts"],
                "target_long": row["target_long"],
                "target_short": row["target_short"],
                "target_gross": row["target_gross"],
                "actual_long": row["actual_long"],
                "actual_short": row["actual_short"],
                "actual_gross": row["actual_gross"],
                "by_sleeve": json.loads(row["actual_by_sleeve"]),
                "largest_symbol_gaps": json.loads(row["largest_symbol_gaps"]),
            }

    leverage = None
    leverage_columns = _table_columns(conn, "leverage_recommendations")
    if {
        "ts", "mode", "observations", "target_vol", "realized_vol",
        "recommended_leverage", "ready", "reason",
    }.issubset(leverage_columns):
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ts, mode, observations, target_vol, realized_vol, "
            "recommended_leverage, ready, reason "
            "FROM leverage_recommendations WHERE ts > ? "
            "ORDER BY ts DESC LIMIT 1",
            (since,),
        ).fetchone()
        if row:
            leverage = {
                "ts": row["ts"],
                "mode": row["mode"],
                "observations": row["observations"],
                "target_vol": row["target_vol"],
                "realized_vol": row["realized_vol"],
                "recommended_leverage": row["recommended_leverage"],
                "ready": bool(row["ready"]),
                "reason": row["reason"],
            }

    return {
        "overall": summarize(orders),
        "by_sleeve": by_sleeve,
        "rejections": {
            "count": len(rejection_rows),
            "requested_notional": round(
                sum(float(row["requested_notional"] or 0) for row in rejection_rows),
                2,
            ),
            "whole_share_rounding": sum(
                "rounds below 1 whole share" in str(row["reason"])
                for row in rejection_rows
            ),
            "hard_to_borrow": sum(
                "hard to borrow" in str(row["reason"])
                for row in rejection_rows
            ),
        },
        "latest_exposure": exposure,
        "latest_leverage_recommendation": leverage,
    }
