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
    """Experiment-tier status from risk_state*.json — the stand-down set
    and per-experiment realized P&L the daily runners persist. A stood-down
    experiment is a loud, human-actionable state that was previously
    invisible in the UI."""
    return {
        "standdowns": sorted(risk_state.get("experiment_standdowns", []) or []),
        "realized_pnl": risk_state.get("experiment_realized_pnl", {}) or {},
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

    equity = None
    last_run_ts = None
    latest_leverage = None
    execution = None
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

    budget = risk_budget(
        cfg.risk.daily_loss_limit_pct,
        cfg.risk.monthly_kill_switch_pct,
        cfg.risk.peak_drawdown_halt_pct,
        risk_state,
        equity,
    )
    cooldown = reentry_cooldown(risk_state, cfg.risk.loss_reentry_block_days, dt.date.today())
    overlay_cfg = cfg.sleeves_paper.get("volatility_overlay", {}) if cfg.sleeves_paper else {}

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
        "experiments": experiments_status(risk_state),
        "health": health,
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
        return {"latest_exposure": None}
    with contextlib.closing(conn):
        es = execution_summary(conn, EPOCH)
    return {"latest_exposure": es["latest_exposure"]}


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
