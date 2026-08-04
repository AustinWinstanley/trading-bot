"""Read-only data access for the dashboard.

Every function here either opens a SQLite connection in genuine read-only
mode or reads a JSON file the cron jobs already write. Nothing in this
module can write to state/paper*.db, and nothing here calls Alpaca or reads
.env — see engine/attribution.py (imported, confirmed pure) for the two
heavier aggregations this module builds on.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Mirrors scripts.run_daily.PROFILES, deliberately not imported from there —
# importing scripts.run_daily would pull in its module-level mutable globals
# (DB/RISK_STATE/REPORT_DIR set by set_profile()), which are fine for a
# one-shot cron script but unsafe to share across a multi-request server.
PROFILES: dict[str, tuple[str, str]] = {
    "base": ("config.yaml", ""),
    "2x": ("config_2x.yaml", "_2x"),
}

# A cutoff well before any real journal row, used as the default "since" so
# a first poll (no cursor yet) returns "everything there is."
EPOCH = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class ProfilePaths:
    profile: str
    config_path: Path
    db_path: Path
    risk_state_path: Path
    health_status_path: Path


def profile_paths(repo_root: Path, profile: str) -> ProfilePaths:
    cfg_file, state_suffix = PROFILES[profile]
    state = repo_root / "state"
    return ProfilePaths(
        profile=profile,
        config_path=repo_root / cfg_file,
        db_path=state / f"paper{state_suffix}.db",
        risk_state_path=state / f"risk_state{state_suffix}.json",
        health_status_path=state / f"health_status{state_suffix}.json",
    )


def open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite file in genuinely read-only mode.

    A mode=ro URI connection cannot write, and cannot create the file if it
    is missing (unlike a normal connect()) — it raises sqlite3.OperationalError
    instead, which callers treat as "this profile hasn't produced a journal
    yet" rather than silently creating an empty one.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT ts, equity, cash, positions, diag FROM snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "ts": row["ts"],
        "equity": row["equity"],
        "cash": row["cash"],
        "positions": json.loads(row["positions"] or "{}"),
        "diag": json.loads(row["diag"] or "{}"),
    }


def equity_curve(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """One point per calendar day (that day's last snapshot).

    The daily job fires more than once a session (AGENTS.md), so a raw
    row-per-run series would chart intraday polling noise rather than
    day-over-day change; this matches how the rest of the repo already
    treats "the run of record" for a date.
    """
    rows = conn.execute("SELECT ts, equity, cash FROM snapshots ORDER BY ts").fetchall()
    by_day: dict[str, sqlite3.Row] = {}
    for row in rows:
        by_day[str(row["ts"])[:10]] = row  # ts-ascending, so last write wins
    ordered_days = sorted(by_day)[-days:]
    return [
        {
            "date": day,
            "ts": by_day[day]["ts"],
            "equity": by_day[day]["equity"],
            "cash": by_day[day]["cash"],
        }
        for day in ordered_days
    ]


_ORDER_BASE_COLUMNS = (
    "ts", "symbol", "side", "sleeve", "qty", "notional",
    "limit_price", "stop_price", "reason", "alpaca_id", "status",
)
_ORDER_MIGRATION_COLUMNS = (
    "requested_notional", "reference_price", "filled_qty",
    "filled_avg_price", "filled_at",
)


def recent_orders(conn: sqlite3.Connection, since: str | None, limit: int = 200) -> dict:
    """Tail-polled order feed. Tolerates the newer migration columns being
    absent (an un-migrated deployment) — see engine/attribution.py for the
    same PRAGMA-driven tolerance pattern."""
    cols = table_columns(conn, "orders")
    select_cols = [c for c in _ORDER_BASE_COLUMNS if c in cols]
    select_cols += [c for c in _ORDER_MIGRATION_COLUMNS if c in cols]
    query = f"SELECT {', '.join(select_cols)} FROM orders"
    params: list = []
    if since:
        query += " WHERE ts > ?"
        params.append(since)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    latest_ts = rows[0]["ts"] if rows else since
    orders = [dict(row) for row in reversed(rows)]  # oldest first for feed rendering
    return {"orders": orders, "latest_ts": latest_ts}


def unstopped_from_journal(conn: sqlite3.Connection, exempt_sleeves: frozenset[str]) -> set[str]:
    """Duplicated from scripts/healthcheck.py::unstopped_from_journal.

    Deliberately NOT imported: importing scripts.healthcheck would
    transitively pull in engine.execute.Trader (the Alpaca client), which
    this package must never touch, even as an unused import. Keep this in
    sync by hand if the source function changes.
    """
    if not exempt_sleeves:
        return set()
    rows = conn.execute(
        "SELECT symbol, sleeve FROM orders WHERE side IN ('buy','short') "
        "AND ts = (SELECT MAX(ts) FROM orders o2 WHERE o2.symbol = orders.symbol "
        "          AND o2.side IN ('buy','short'))"
    ).fetchall()
    return {str(row["symbol"]) for row in rows if str(row["sleeve"]) in exempt_sleeves}


def current_positions(conn: sqlite3.Connection, stop_exempt_sleeves: frozenset[str]) -> list[dict]:
    """Current positions with stop levels and unrealized P&L where derivable.

    P&L needs an entry price, which only exists in the local `stops` table.
    Stop-exempt-sleeve positions (mom_ls) have no such row by design (see
    config.yaml's stop_exempt_sleeves) — those show cost_basis_available:
    false rather than a fabricated number.
    """
    snap = latest_snapshot(conn)
    if snap is None:
        return []
    positions = snap["positions"]
    origin = snap["diag"].get("origin", {})
    stop_rows = {
        str(row["symbol"]): row
        for row in conn.execute(
            "SELECT symbol, stop_price, entry_price, entry_date, sleeve FROM stops"
        )
    }
    out = []
    for symbol, info in positions.items():
        qty = float(info.get("qty", 0.0))
        px = float(info.get("px", 0.0))
        stop = stop_rows.get(symbol)
        sleeve = str(origin.get(symbol, ""))
        exempt = bool(sleeve) and all(
            part in stop_exempt_sleeves for part in sleeve.split("+")
        )
        entry_price = float(stop["entry_price"]) if stop and stop["entry_price"] is not None else None
        unrealized_pl = (px - entry_price) * qty if entry_price is not None else None
        out.append({
            "symbol": symbol,
            "qty": qty,
            "price": px,
            "market_value": round(qty * px, 2),
            "sleeve": sleeve,
            "stop_price": float(stop["stop_price"]) if stop else None,
            "entry_price": entry_price,
            "unrealized_pl": round(unrealized_pl, 2) if unrealized_pl is not None else None,
            "cost_basis_available": entry_price is not None,
            "stop_exempt_sleeve": exempt,
        })
    out.sort(key=lambda p: -abs(p["market_value"]))
    return out


def risk_budget(daily_pct: float, monthly_pct: float, peak_pct: float,
                 risk_state: dict, equity: float | None) -> dict | None:
    """% of each loss/drawdown budget consumed, as burndown-bar fractions.

    Config's *_pct fields are positive magnitudes (e.g. 0.04 for a 4% daily
    loss budget), validated as such by engine.config._fraction's (0, 1]
    bound — not signed deltas.
    """
    if equity is None:
        return None

    def used(start: float | None, limit_pct: float) -> float | None:
        if not start or start <= 0 or limit_pct <= 0:
            return None
        drawdown = max(0.0, (start - equity) / start)
        return round(min(drawdown / limit_pct, 1.5) * 100, 1)

    return {
        "daily_loss_limit_pct": round(daily_pct * 100, 2),
        "daily_used_pct": used(risk_state.get("day_start_equity"), daily_pct),
        "monthly_kill_switch_pct": round(monthly_pct * 100, 2),
        "monthly_used_pct": used(risk_state.get("month_start_equity"), monthly_pct),
        "peak_drawdown_halt_pct": round(peak_pct * 100, 2),
        "peak_used_pct": used(risk_state.get("peak_equity"), peak_pct),
    }


def reentry_cooldown(risk_state: dict, cooldown_days: int, today: dt.date) -> list[dict]:
    out = []
    for symbol, exit_date_str in risk_state.get("recent_losses", {}).items():
        try:
            exit_date = dt.date.fromisoformat(str(exit_date_str))
        except ValueError:
            continue
        days_remaining = cooldown_days - (today - exit_date).days
        if days_remaining > 0:
            out.append({
                "symbol": symbol,
                "exit_date": exit_date_str,
                "days_remaining": days_remaining,
            })
    out.sort(key=lambda r: -r["days_remaining"])
    return out


_NUMERIC_RUN = re.compile(r"-?\d[\d,]*\.?\d*")


def normalize_reason(reason: str) -> str:
    """Collapse embedded numbers so near-identical gate-rejection reasons
    group together (engine/risk.py's reasons are f-strings with live
    numbers baked in, e.g. "price 12.34 below minimum 5.00")."""
    return _NUMERIC_RUN.sub("#", reason)


def top_rejection_reasons(conn: sqlite3.Connection, since: str, limit: int = 10) -> list[dict]:
    cols = table_columns(conn, "rejections")
    if not {"ts", "reason"}.issubset(cols):
        return []
    rows = conn.execute("SELECT reason FROM rejections WHERE ts > ?", (since,)).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        template = normalize_reason(str(row["reason"]))
        counts[template] = counts.get(template, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: -item[1])[:limit]
    return [{"reason": reason, "count": count} for reason, count in ranked]
