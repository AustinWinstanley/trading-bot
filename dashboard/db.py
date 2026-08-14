"""Read-only data access for the dashboard.

Every function here either opens a SQLite connection in genuine read-only
mode or reads a JSON file the cron jobs already write. Nothing in this
module can write to state/paper*.db, and nothing here calls Alpaca or reads
.env — see engine/attribution.py and engine/config.py (imported, confirmed
pure) for the two heavier building blocks the *_payload functions below
compose.

The *_payload functions (summary_payload, equity_curve_payload, etc.) are
the single source of truth for "what each API response means" — both
dashboard/routes.py (Flask JSON endpoints) and mcp_server/tools.py (MCP
tools for live agent access to the same server) call these same functions
rather than each having their own copy of the composition logic. Keeping
this in one place is deliberate: a fix applied to one caller and not the
other is exactly the kind of drift that caused a real production incident
(engine.config.load_config's validate_experiments divergence, 2026-08-13).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.attribution import execution_summary
from engine.config import load_config

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
    options_db_path: Path


def profile_paths(repo_root: Path, profile: str) -> ProfilePaths:
    cfg_file, state_suffix = PROFILES[profile]
    state = repo_root / "state"
    return ProfilePaths(
        profile=profile,
        config_path=repo_root / cfg_file,
        db_path=state / f"paper{state_suffix}.db",
        risk_state_path=state / f"risk_state{state_suffix}.json",
        health_status_path=state / f"health_status{state_suffix}.json",
        # Written by scripts/options_daily.py (2x lab only today; the suffix
        # keeps this correct if base ever gets one). May be absent or a
        # 0-byte file — callers must tolerate both.
        options_db_path=state / f"options{state_suffix}.db",
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
    """One point per calendar day (that day's last snapshot), with derived
    day-over-day P&L, cumulative window return, and drawdown-from-peak.

    The daily job fires more than once a session (AGENTS.md), so a raw
    row-per-run series would chart intraday polling noise rather than
    day-over-day change; this matches how the rest of the repo already
    treats "the run of record" for a date.

    Derivations are computed over the FULL history and only then sliced
    to the window: drawdown uses the true running all-history peak (a
    window that starts mid-drawdown must not report 0%), and pnl's first
    windowed point still knows the prior day's equity. return_pct alone
    is window-relative by design — "how has it done over the period I'm
    looking at."
    """
    rows = conn.execute("SELECT ts, equity, cash FROM snapshots ORDER BY ts").fetchall()
    by_day: dict[str, sqlite3.Row] = {}
    for row in rows:
        by_day[str(row["ts"])[:10]] = row  # ts-ascending, so last write wins
    all_days = sorted(by_day)

    points = []
    peak = None
    prev_equity = None
    for day in all_days:
        row = by_day[day]
        equity = row["equity"]
        peak = equity if peak is None else max(peak, equity)
        points.append({
            "date": day,
            "ts": row["ts"],
            "equity": equity,
            "cash": row["cash"],
            "pnl": round(equity - prev_equity, 2) if prev_equity is not None else None,
            "drawdown_pct": (
                round((equity - peak) / peak * 100, 3) if peak else 0.0
            ),
        })
        prev_equity = equity

    window = points[-days:]
    base = window[0]["equity"] if window else None
    for point in window:
        point["return_pct"] = (
            round((point["equity"] - base) / base * 100, 3) if base else None
        )
    return window


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
            "entry_date": str(stop["entry_date"]) if stop and stop["entry_date"] else None,
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


def rejections_by_sleeve_side(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Which sleeve/side combinations the gate is blocking, and how much
    notional it blocked. Pre-migration rows carry NULL sleeve/side (the
    majority of historical rows) — grouped as "untagged" rather than
    silently dropped, so the skew itself stays visible."""
    cols = table_columns(conn, "rejections")
    if not {"ts", "reason"}.issubset(cols):
        return []
    has_sleeve = "sleeve" in cols
    has_side = "side" in cols
    has_notional = "requested_notional" in cols
    sleeve_expr = "COALESCE(sleeve, '(untagged)')" if has_sleeve else "'(untagged)'"
    side_expr = "COALESCE(side, '')" if has_side else "''"
    notional_expr = "COALESCE(SUM(requested_notional), 0)" if has_notional else "0"
    rows = conn.execute(
        f"SELECT {sleeve_expr} AS sleeve, {side_expr} AS side, "
        f"COUNT(*) AS count, {notional_expr} AS blocked_notional "
        "FROM rejections WHERE ts > ? GROUP BY 1, 2 ORDER BY count DESC",
        (since,),
    ).fetchall()
    return [
        {
            "sleeve": str(row["sleeve"]),
            "side": str(row["side"]),
            "count": int(row["count"]),
            "blocked_notional": round(float(row["blocked_notional"]), 2),
        }
        for row in rows
    ]


def experiments_status(risk_state: dict) -> dict:
    """Experiment-tier status from risk_state*.json — the stand-down set,
    per-experiment realized P&L, and the consecutive buying-power-miss
    counters (scripts/options_daily.py) the daily runners persist. A
    stood-down experiment or a multi-day buying-power stall is a loud,
    human-actionable state that was previously invisible in the UI."""
    return {
        "standdowns": sorted(risk_state.get("experiment_standdowns", []) or []),
        "realized_pnl": risk_state.get("experiment_realized_pnl", {}) or {},
        "buying_power_misses": risk_state.get("experiment_buying_power_misses", {}) or {},
    }


_STRUCTURE_COLUMNS = (
    "structure_id", "experiment", "strategy", "underlying", "expiration_date",
    "contracts", "requested_contracts", "credit", "maximum_loss",
    "opened_ts", "open_status", "open_filled_at", "close_reason", "closed_ts",
    "close_status", "realized_pnl", "status",
)


def options_structures(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Recent multi-leg options structures from state/options_*.db, with
    their legs attached. Tolerates a 0-byte or pre-migration database
    (scripts/options_daily.py creates tables lazily on its first run)."""
    cols = table_columns(conn, "structures")
    if not cols:
        return []
    select_cols = [c for c in _STRUCTURE_COLUMNS if c in cols]
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM structures "
        "ORDER BY opened_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    structures = [dict(row) for row in rows]
    leg_cols = table_columns(conn, "structure_legs")
    if {"structure_id", "symbol", "side", "position_intent"}.issubset(leg_cols):
        for structure in structures:
            legs = conn.execute(
                "SELECT symbol, side, position_intent, ratio_qty "
                "FROM structure_legs WHERE structure_id = ?",
                (structure["structure_id"],),
            ).fetchall()
            structure["legs"] = [dict(leg) for leg in legs]
    return structures


def reconciliation_events(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Broker-vs-journal mismatch alarms from scripts/options_daily.py's
    assignment-detection backstop — including possible early assignment.
    Anything here is a page, not a curiosity."""
    cols = table_columns(conn, "reconciliation_events")
    if not {"ts", "severity", "detail"}.issubset(cols):
        return []
    rows = conn.execute(
        "SELECT ts, severity, structure_id, symbol, detail "
        "FROM reconciliation_events WHERE ts > ? ORDER BY ts DESC LIMIT 50",
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------
# Time-series analytics — per-day views of data execution_summary only
# aggregates all-time. Plain SQL + stdlib arithmetic; missing migration
# columns degrade per-field to None, never an error.
# ---------------------------------------------------------------------


def execution_trends(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Per-day execution/rejection quality — the time-series version of
    engine.attribution.execution_summary's single all-time aggregate.
    Slippage is notional-weighted with the same sign convention (adverse
    positive), latency is filled_at - ts in seconds."""
    order_cols = table_columns(conn, "orders")
    have_fills = {"filled_avg_price", "reference_price", "filled_at"}.issubset(order_cols)

    days: dict[str, dict] = {}

    def bucket(date: str) -> dict:
        return days.setdefault(date, {
            "date": date, "orders": 0, "filled_orders": 0, "fill_pct": None,
            "adverse_slippage_bps": None, "avg_latency_s": None,
            "rejections": 0, "blocked_notional": 0.0,
            "_slip_weighted": 0.0, "_slip_notional": 0.0,
            "_latency_total": 0.0, "_latency_n": 0,
        })

    select = "ts, side, notional, status"
    if have_fills:
        select += ", filled_avg_price, reference_price, filled_at"
    for row in conn.execute(f"SELECT {select} FROM orders WHERE ts > ? ORDER BY ts", (since,)):
        b = bucket(str(row["ts"])[:10])
        b["orders"] += 1
        if str(row["status"] or "") == "filled":
            b["filled_orders"] += 1
        if not have_fills:
            continue
        fill_px, ref_px = row["filled_avg_price"], row["reference_price"]
        if fill_px and ref_px:
            direction = 1 if row["side"] in ("buy", "cover") else -1
            slip_bps = (fill_px - ref_px) / ref_px * direction * 10000
            notional = float(row["notional"] or 0.0)
            b["_slip_weighted"] += slip_bps * notional
            b["_slip_notional"] += notional
        filled_at = _parse_ts(row["filled_at"])
        ts = _parse_ts(row["ts"])
        if filled_at and ts and filled_at >= ts:
            b["_latency_total"] += (filled_at - ts).total_seconds()
            b["_latency_n"] += 1

    rej_cols = table_columns(conn, "rejections")
    have_rej_notional = "requested_notional" in rej_cols
    select = "ts" + (", requested_notional" if have_rej_notional else "")
    for row in conn.execute(f"SELECT {select} FROM rejections WHERE ts > ?", (since,)):
        b = bucket(str(row["ts"])[:10])
        b["rejections"] += 1
        if have_rej_notional and row["requested_notional"]:
            b["blocked_notional"] += float(row["requested_notional"])

    out = []
    for date in sorted(days):
        b = days[date]
        if b["orders"]:
            b["fill_pct"] = round(b["filled_orders"] / b["orders"] * 100, 1)
        if b["_slip_notional"] > 0:
            b["adverse_slippage_bps"] = round(b["_slip_weighted"] / b["_slip_notional"], 2)
        if b["_latency_n"]:
            b["avg_latency_s"] = round(b["_latency_total"] / b["_latency_n"], 1)
        b["blocked_notional"] = round(b["blocked_notional"], 2)
        out.append({k: v for k, v in b.items() if not k.startswith("_")})
    return out


# Fill columns only exist in journal rows written since this date (the
# order-journal migration) — part of the round-trips contract so the UI
# says so instead of implying full history.
_FILL_COVERAGE_NOTE = "fills recorded since 2026-08-04"

_ENTRY_SIDES = frozenset({"buy", "short"})
_EXIT_SIDES = frozenset({"sell", "cover"})


def round_trips(conn: sqlite3.Connection, limit: int = 100) -> dict:
    """FIFO-matched realized P&L per closed round trip, from order fills
    ONLY (filled_qty/filled_avg_price) — deliberately not stops.entry_price,
    since stops rows are mutated and deleted over a position's life while
    the fill columns are the immutable record. Exits with no recorded
    entry (position predates fill recording) are counted in "unmatched",
    never guessed at."""
    cols = table_columns(conn, "orders")
    if not {"filled_qty", "filled_avg_price"}.issubset(cols):
        return {"trips": [], "by_sleeve": {}, "unmatched": 0,
                "coverage_note": _FILL_COVERAGE_NOTE}

    rows = conn.execute(
        "SELECT ts, symbol, side, sleeve, filled_qty, filled_avg_price FROM orders "
        "WHERE COALESCE(status,'') = 'filled' AND filled_qty > 0 "
        "AND filled_avg_price IS NOT NULL ORDER BY ts"
    ).fetchall()

    # Per symbol: open FIFO lots [(qty_remaining, price, ts, sleeve, side)].
    lots: dict[str, list[dict]] = {}
    trips: list[dict] = []
    unmatched = 0
    for row in rows:
        symbol = str(row["symbol"])
        side = str(row["side"])
        qty = float(row["filled_qty"])
        price = float(row["filled_avg_price"])
        if side in _ENTRY_SIDES:
            lots.setdefault(symbol, []).append({
                "qty": qty, "price": price, "ts": row["ts"],
                "sleeve": row["sleeve"], "side": side,
            })
            continue
        if side not in _EXIT_SIDES:
            continue
        remaining = qty
        symbol_lots = lots.get(symbol, [])
        while remaining > 1e-9 and symbol_lots:
            lot = symbol_lots[0]
            matched = min(remaining, lot["qty"])
            # Long round trip: sell exits a buy; short: cover exits a short.
            direction = 1 if lot["side"] == "buy" else -1
            pnl = (price - lot["price"]) * matched * direction
            trips.append({
                "symbol": symbol,
                "sleeve": str(lot["sleeve"] or ""),
                "entry_ts": lot["ts"],
                "exit_ts": row["ts"],
                "qty": round(matched, 6),
                "entry_price": lot["price"],
                "exit_price": price,
                "realized_pnl": round(pnl, 2),
            })
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 1e-9:
                symbol_lots.pop(0)
        if remaining > 1e-9:
            unmatched += 1

    by_sleeve: dict[str, dict] = {}
    for trip in trips:
        agg = by_sleeve.setdefault(trip["sleeve"] or "(untagged)", {
            "trips": 0, "realized_pnl": 0.0, "wins": 0, "losses": 0,
        })
        agg["trips"] += 1
        agg["realized_pnl"] = round(agg["realized_pnl"] + trip["realized_pnl"], 2)
        if trip["realized_pnl"] >= 0:
            agg["wins"] += 1
        else:
            agg["losses"] += 1

    return {
        "trips": trips[-limit:][::-1],  # newest first
        "by_sleeve": by_sleeve,
        "unmatched": unmatched,
        "coverage_note": _FILL_COVERAGE_NOTE,
    }


def exposure_history(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """Target-vs-actual gross exposure over time from attribution_snapshots
    — the table engine.attribution only ever reads LIMIT 1 from. One point
    per calendar day (last write wins), same dedup as equity_curve."""
    cols = table_columns(conn, "attribution_snapshots")
    if not {"ts", "target_gross", "actual_gross"}.issubset(cols):
        return []
    rows = conn.execute(
        "SELECT ts, target_gross, actual_gross, target_long, target_short, "
        "actual_long, actual_short FROM attribution_snapshots ORDER BY ts"
    ).fetchall()
    by_day: dict[str, sqlite3.Row] = {}
    for row in rows:
        by_day[str(row["ts"])[:10]] = row
    return [
        {
            "date": day,
            "target_gross": by_day[day]["target_gross"],
            "actual_gross": by_day[day]["actual_gross"],
            "target_net": (
                round(by_day[day]["target_long"] - by_day[day]["target_short"], 4)
                if by_day[day]["target_long"] is not None
                and by_day[day]["target_short"] is not None else None
            ),
            "actual_net": (
                round(by_day[day]["actual_long"] - by_day[day]["actual_short"], 4)
                if by_day[day]["actual_long"] is not None
                and by_day[day]["actual_short"] is not None else None
            ),
        }
        for day in sorted(by_day)[-days:]
    ]


# ---------------------------------------------------------------------
# Attention signals — "something needs a look" states, each with a real
# incident behind it (see each docstring). All feed the "attention" key
# on /summary; each takes `now` explicitly for testability and degrades
# to no-signal (empty list) on missing data, never an exception.
# ---------------------------------------------------------------------


def _parse_ts(value) -> dt.datetime | None:
    """Tolerant ISO timestamp parse -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def stuck_new_orders(
    conn: sqlite3.Connection, now: dt.datetime, threshold_minutes: int = 30
) -> list[dict]:
    """Orders sitting at status='new' past the threshold — the 2026-08-13
    BE/HUT case: two limit orders stuck 'new' all day while the broker-side
    healthcheck reported open_orders: 0 (it counts broker state, this
    counts journal state; when they disagree, someone should look)."""
    if "status" not in table_columns(conn, "orders"):
        return []
    rows = conn.execute(
        "SELECT symbol, ts FROM orders WHERE COALESCE(status,'') = 'new' ORDER BY ts"
    ).fetchall()
    stuck = []
    for row in rows:
        ts = _parse_ts(row["ts"])
        if ts is None:
            continue
        age_minutes = (now.astimezone(dt.timezone.utc) - ts).total_seconds() / 60
        if age_minutes >= threshold_minutes:
            stuck.append((str(row["symbol"]), int(age_minutes)))
    if not stuck:
        return []
    detail = ", ".join(f"{sym} ({age}m)" for sym, age in stuck[:8])
    return [{
        "id": "stuck_new_orders",
        "severity": "danger",
        "message": f"{len(stuck)} order(s) stuck at status 'new': {detail}",
    }]


def health_staleness(
    health: dict | None, now: dt.datetime, max_age_hours: float = 26.0
) -> list[dict]:
    """A healthcheck that stopped running still shows its last verdict —
    on 2026-08-14 the live dashboard rendered a >24h-old HEALTHY as a green
    dot with no age check at all. 26h allows one full day between the
    scheduled post-close checks plus slack."""
    if not health:
        return []
    ts = _parse_ts(health.get("ts"))
    if ts is None:
        return []
    age_hours = (now.astimezone(dt.timezone.utc) - ts).total_seconds() / 3600
    if age_hours < max_age_hours:
        return []
    return [{
        "id": "health_stale",
        "severity": "danger",
        "message": (
            f"health check last ran {age_hours:.0f}h ago — its "
            f"'{'healthy' if health.get('healthy') else 'unhealthy'}' verdict is stale"
        ),
    }]


def _us_market_likely_open(now: dt.datetime) -> bool:
    """Weekday 09:30-16:00 ET approximation via zoneinfo. Deliberately no
    holiday calendar: a market holiday produces one spurious warn-level
    staleness signal, which is the cheap side of that tradeoff."""
    from zoneinfo import ZoneInfo

    et = now.astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


def last_run_staleness(
    last_run_ts, now: dt.datetime, max_age_hours: float = 4.0
) -> list[dict]:
    """During market hours, a journal whose last snapshot is hours old
    means scheduled runs are silently not happening (cron broken, lock
    stuck, server down). Scheduled full runs are ~3h apart (09:47/12:35 ET
    + stops-only checks that snapshot nothing), so 4h means at least one
    full run was missed."""
    if not _us_market_likely_open(now):
        return []
    ts = _parse_ts(last_run_ts)
    if ts is None:
        return []
    age_hours = (now.astimezone(dt.timezone.utc) - ts).total_seconds() / 3600
    if age_hours < max_age_hours:
        return []
    return [{
        "id": "last_run_stale",
        "severity": "warn",
        "message": (
            f"last journal snapshot is {age_hours:.1f}h old during market hours — "
            "scheduled runs may not be executing"
        ),
    }]


def mom_ls_targets_staleness(repo_root: Path, cfg, now: dt.datetime) -> list[dict]:
    """A missing or stale mom_ls targets file silently stands the whole
    sleeve down (engine/portfolio.py:mom_ls_targets returns {}), which on
    2026-08-13 liquidated the 2x lab's entire mom_ls book with no surface
    anywhere. Path and max age come from the profile's own config."""
    paper = cfg.sleeves_paper or {}
    rel_path = paper.get("mom_ls_targets_file")
    if not rel_path or not paper.get("sleeves", {}).get("mom_ls"):
        return []  # sleeve not configured for this profile
    path = repo_root / rel_path
    if not path.exists():
        return [{
            "id": "mom_ls_targets_missing",
            "severity": "danger",
            "message": (
                f"mom_ls targets file {rel_path} is missing — the sleeve is "
                "silently standing down (holds no positions, closes existing ones)"
            ),
        }]
    max_age_days = int(paper.get("mom_ls_max_age_days", 10))
    data = load_json(path, default=None)
    as_of = None if not isinstance(data, dict) else data.get("as_of")
    if not as_of:
        return []
    try:
        age_days = (now.astimezone(dt.timezone.utc).date() - dt.date.fromisoformat(as_of)).days
    except ValueError:
        return []
    if age_days <= max_age_days:
        return []
    return [{
        "id": "mom_ls_targets_stale",
        "severity": "danger",
        "message": (
            f"mom_ls targets file is {age_days}d old (max {max_age_days}d) — "
            "the sleeve is silently standing down"
        ),
    }]


def options_db_zero_byte(paths: ProfilePaths) -> list[dict]:
    """A 0-byte options DB means the file was touched but the first
    options_daily run never created the schema — a different state from
    healthy-but-idle, and currently rendered identically to it."""
    try:
        if paths.options_db_path.exists() and paths.options_db_path.stat().st_size == 0:
            return [{
                "id": "options_db_empty_file",
                "severity": "warn",
                "message": (
                    f"{paths.options_db_path.name} exists but is 0 bytes — "
                    "the options journal schema has never been created"
                ),
            }]
    except OSError:
        pass
    return []


def buying_power_miss_signals(experiments: dict) -> list[dict]:
    """Consecutive buying-power misses tracked by scripts/options_daily.py
    — a nonzero streak means the options experiment wants to trade and
    can't afford to, which otherwise only shows in the daily log."""
    signals = []
    for name, misses in sorted((experiments.get("buying_power_misses") or {}).items()):
        if misses and int(misses) > 0:
            signals.append({
                "id": f"buying_power_misses:{name}",
                "severity": "warn",
                "message": (
                    f"experiment {name}: {int(misses)} consecutive day(s) short of "
                    "options buying power"
                ),
            })
    return signals


def attention_signals(
    *,
    journal_signals: list[dict],
    health: dict | None,
    last_run_ts,
    repo_root: Path,
    cfg,
    paths: ProfilePaths,
    experiments: dict,
    now: dt.datetime | None = None,
) -> dict:
    """Aggregate every attention signal for /summary's "attention" key.
    journal_signals (e.g. stuck_new_orders) are computed by the caller
    while its journal connection is still open. Order: dangers first
    (stable within severity by check order)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    signals: list[dict] = list(journal_signals)
    signals += health_staleness(health, now)
    signals += last_run_staleness(last_run_ts, now)
    signals += mom_ls_targets_staleness(repo_root, cfg, now)
    signals += options_db_zero_byte(paths)
    signals += buying_power_miss_signals(experiments)
    signals.sort(key=lambda s: 0 if s["severity"] == "danger" else 1)
    return {"signals": signals}


# ---------------------------------------------------------------------
# Composed payloads — one function per API response shape, shared by
# dashboard/routes.py and mcp_server/tools.py. See this module's
# docstring for why these exist as a single source of truth.
# ---------------------------------------------------------------------


def _open_ro_or_none(paths: ProfilePaths) -> sqlite3.Connection | None:
    try:
        return open_ro(paths.db_path)
    except sqlite3.OperationalError:
        # No journal yet for this profile — a valid, expected state right
        # after a fresh checkout, not an error.
        return None


def summary_payload(repo_root: Path, profile: str) -> dict:
    paths = profile_paths(repo_root, profile)
    # validate_experiments=False: some deployments of this data layer (the
    # dashboard container) never mount reports/, and a monitoring response
    # must never fail because a trading-safety business rule (registration
    # doc must exist) is unmet — that's a real state to report, not a
    # reason to error out. See engine/config.py's _parse_experiments
    # docstring.
    cfg = load_config(paths.config_path, validate_experiments=False)
    risk_state = load_json(paths.risk_state_path, default={})
    health = load_json(paths.health_status_path, default=None)

    now = dt.datetime.now(dt.timezone.utc)
    equity = None
    last_run_ts = None
    latest_leverage = None
    execution = None
    journal_signals: list[dict] = []
    conn = _open_ro_or_none(paths)
    if conn is not None:
        with contextlib.closing(conn):
            snap = latest_snapshot(conn)
            if snap:
                equity = snap["equity"]
                last_run_ts = snap["ts"]
            es = execution_summary(conn, EPOCH)
            latest_leverage = es["latest_leverage_recommendation"]
            # Computed by the call above since day one and previously
            # discarded — the whole fill-quality story (fill %, approval %,
            # adverse slippage bps, overall and per sleeve) for free.
            execution = {"overall": es["overall"], "by_sleeve": es["by_sleeve"]}
            journal_signals = stuck_new_orders(conn, now)

    budget = risk_budget(
        cfg.risk.daily_loss_limit_pct,
        cfg.risk.monthly_kill_switch_pct,
        cfg.risk.peak_drawdown_halt_pct,
        risk_state,
        equity,
    )
    cooldown = reentry_cooldown(risk_state, cfg.risk.loss_reentry_block_days, dt.date.today())
    overlay_cfg = cfg.sleeves_paper.get("volatility_overlay", {}) if cfg.sleeves_paper else {}
    experiments = experiments_status(risk_state)

    return {
        "profile": profile,
        "mode": cfg.mode,
        "gross_leverage": cfg.sleeves_paper.get("gross_leverage") if cfg.sleeves_paper else None,
        "halted": bool(risk_state.get("halted", False)),
        "equity": equity,
        "last_run_ts": last_run_ts,
        "risk_budget": budget,
        "reentry_cooldown": cooldown,
        "volatility_overlay": {
            "configured_mode": overlay_cfg.get("mode", "off"),
            "min_observations": overlay_cfg.get("min_observations"),
            "latest_recommendation": latest_leverage,
        },
        "execution": execution,
        "experiments": experiments,
        "health": health,
        "attention": attention_signals(
            journal_signals=journal_signals,
            health=health,
            last_run_ts=last_run_ts,
            repo_root=repo_root,
            cfg=cfg,
            paths=paths,
            experiments=experiments,
            now=now,
        ),
    }


def equity_curve_payload(repo_root: Path, profile: str, days: int) -> dict:
    paths = profile_paths(repo_root, profile)
    cfg = load_config(paths.config_path, validate_experiments=False)
    risk_state = load_json(paths.risk_state_path, default={})

    points: list[dict] = []
    conn = _open_ro_or_none(paths)
    if conn is not None:
        with contextlib.closing(conn):
            points = equity_curve(conn, days=days)

    peak = risk_state.get("peak_equity")
    month_start = risk_state.get("month_start_equity")
    return {
        "points": points,
        "reference_lines": {
            "peak_drawdown_halt": (
                round(peak * (1 - cfg.risk.peak_drawdown_halt_pct), 2) if peak else None
            ),
            "monthly_kill_switch": (
                round(month_start * (1 - cfg.risk.monthly_kill_switch_pct), 2)
                if month_start else None
            ),
        },
    }


def orders_payload(repo_root: Path, profile: str, since: str | None, limit: int) -> dict:
    paths = profile_paths(repo_root, profile)
    conn = _open_ro_or_none(paths)
    if conn is None:
        return {"orders": [], "latest_ts": since}
    with contextlib.closing(conn):
        return recent_orders(conn, since=since, limit=limit)


def positions_payload(repo_root: Path, profile: str) -> dict:
    paths = profile_paths(repo_root, profile)
    cfg = load_config(paths.config_path, validate_experiments=False)
    conn = _open_ro_or_none(paths)
    if conn is None:
        return {"positions": []}
    with contextlib.closing(conn):
        pos = current_positions(conn, cfg.risk.stop_exempt_sleeves)
    return {"positions": pos}


def exposure_payload(repo_root: Path, profile: str) -> dict:
    paths = profile_paths(repo_root, profile)
    conn = _open_ro_or_none(paths)
    if conn is None:
        return {"latest_exposure": None, "history": []}
    with contextlib.closing(conn):
        es = execution_summary(conn, EPOCH)
        history = exposure_history(conn)
    return {"latest_exposure": es["latest_exposure"], "history": history}


def trends_payload(repo_root: Path, profile: str, days: int) -> dict:
    paths = profile_paths(repo_root, profile)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    conn = _open_ro_or_none(paths)
    if conn is None:
        return {"days": []}
    with contextlib.closing(conn):
        return {"days": execution_trends(conn, since)}


def round_trips_payload(repo_root: Path, profile: str, limit: int) -> dict:
    paths = profile_paths(repo_root, profile)
    conn = _open_ro_or_none(paths)
    if conn is None:
        return {"trips": [], "by_sleeve": {}, "unmatched": 0,
                "coverage_note": _FILL_COVERAGE_NOTE}
    with contextlib.closing(conn):
        return round_trips(conn, limit=limit)


def rejections_payload(
    repo_root: Path, profile: str, days: int, since: str | None
) -> dict:
    paths = profile_paths(repo_root, profile)
    effective_since = since or (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    ).isoformat()

    conn = _open_ro_or_none(paths)
    if conn is None:
        return {"count": 0, "requested_notional": 0.0,
                 "whole_share_rounding": 0, "hard_to_borrow": 0,
                 "top_reasons": [], "by_sleeve_side": []}
    with contextlib.closing(conn):
        es = execution_summary(conn, effective_since)
        top_reasons = top_rejection_reasons(conn, effective_since)
        by_sleeve_side = rejections_by_sleeve_side(conn, effective_since)
    return {
        **es["rejections"],
        "top_reasons": top_reasons,
        "by_sleeve_side": by_sleeve_side,
    }


def options_payload(repo_root: Path, profile: str, days: int) -> dict:
    """Options structures + reconciliation alarms from state/options_*.db.

    The file may be missing (base profile, or a lab that has never run
    scripts.options_daily), or exist as a 0-byte file with no tables (it
    was touched but the first run hasn't created the schema) — both are
    the same valid "nothing yet" state, never an error.
    """
    paths = profile_paths(repo_root, profile)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

    empty = {"structures": [], "reconciliation_events": []}
    try:
        conn = open_ro(paths.options_db_path)
    except sqlite3.OperationalError:
        return empty
    with contextlib.closing(conn):
        structures = options_structures(conn)
        events = reconciliation_events(conn, since)
    return {"structures": structures, "reconciliation_events": events}
