"""Read-only operational health check for a paper profile."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from engine.config import load_config
from engine.data import REPO_ROOT, load_env
from engine.execute import Trader
from scripts.options_daily import (
    DB as OPTIONS_DB,
    EXPERIMENT_NAME as OPTIONS_EXPERIMENT_NAME,
    equity_qty_explained_by_orders,
    fetch_open_structures,
    reconcile_option_structures,
)
from scripts.run_daily import PROFILES, is_protective_order

ET = ZoneInfo("America/New_York")


def unstopped_from_journal(conn: sqlite3.Connection, exempt_sleeves: frozenset[str]) -> set[str]:
    """Symbols whose most recent entry came from a deliberately unstopped sleeve.

    Keyed on the latest opening order rather than the current target list: a
    position lingers after its sleeve drops it, and until it is closed it is
    still the unstopped position that sleeve opened.
    """
    if not exempt_sleeves:
        return set()
    rows = conn.execute(
        "SELECT symbol, sleeve FROM orders WHERE side IN ('buy','short') "
        "AND ts = (SELECT MAX(ts) FROM orders o2 WHERE o2.symbol = orders.symbol "
        "          AND o2.side IN ('buy','short'))"
    ).fetchall()
    return {str(sym) for sym, sleeve in rows if str(sleeve) in exempt_sleeves}


def open_option_structures(db_path: Path) -> list[dict]:
    """Open options structures for reconciliation, or [] if none journaled.

    scripts.options_daily runs its schema setup before every journal write,
    so a missing file — or a file with no `structures` table (a bare
    ``sqlite3.connect`` creates a 0-byte schema-less file as a side effect
    of connecting, which is exactly how a read-only consumer once broke
    this check) — means options trading has never journaled a structure:
    nothing to reconcile, which is healthy. Opened read-only so this check
    can never itself create or modify the journal.
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='structures'"
        ).fetchone()
        if not has_table:
            return []
        return fetch_open_structures(conn, OPTIONS_EXPERIMENT_NAME)
    finally:
        conn.close()


def assess_health(
    *,
    account_status: str,
    positions: list[dict],
    open_orders: list[dict],
    fallback_stops: set[str],
    last_snapshot: dt.datetime | None,
    now: dt.datetime,
    max_age_hours: float,
    allow_pristine: bool = False,
    journal_is_pristine: bool = False,
    unstopped_symbols: set[str] | None = None,
) -> list[str]:
    problems: list[str] = []
    if account_status != "ACTIVE":
        problems.append(f"account status is {account_status!r}, expected ACTIVE")

    pristine_profile = (
        allow_pristine
        and journal_is_pristine
        and not positions
        and not open_orders
        and not fallback_stops
    )
    if last_snapshot is None and not pristine_profile:
        problems.append("no journal snapshot exists")
    elif last_snapshot is not None:
        if last_snapshot.tzinfo is None:
            last_snapshot = last_snapshot.replace(tzinfo=ET)
        age_hours = (now - last_snapshot.astimezone(now.tzinfo)).total_seconds() / 3600
        if age_hours > max_age_hours:
            problems.append(
                f"last journal snapshot is {age_hours:.1f}h old "
                f"(limit {max_age_hours:.1f}h)"
            )

    protected = {
        str(order.get("symbol"))
        for order in open_orders
        if is_protective_order(order)
    }
    # Positions opened by a sleeve in risk.stop_exempt_sleeves carry no stop by
    # design, so an alert on them would be permanent noise. Everything else
    # missing a stop is still a real problem.
    unstopped = unstopped_symbols or set()
    for position in positions:
        symbol = str(position.get("symbol", ""))
        if not symbol or symbol in unstopped:
            continue
        # Option contracts (scripts/options_daily.py's bull-put spread, this
        # repo's only source of us_option positions) are defined-risk by the
        # spread's own maximum_loss, never by an equity-style stop order —
        # scripts/options_daily.py has no stop-submission path for legs at
        # all. Flagging them here was pure noise from day one of live
        # options trading (2026-08-14). An orphaned or mismatched leg is a
        # real problem, but it's scripts/options_daily.py's
        # reconcile_option_structures's job to catch that (missing-leg /
        # wrong-sign-leg findings), not this equity-shaped check's.
        if str(position.get("asset_class", "")) == "us_option":
            continue
        if symbol not in protected and symbol not in fallback_stops:
            problems.append(f"{symbol}: position has no broker or fallback stop")

    for order in open_orders:
        submitted = order.get("submitted_at")
        if not submitted or is_protective_order(order):
            continue
        try:
            stamp = dt.datetime.fromisoformat(str(submitted).replace("Z", "+00:00"))
            age_hours = (now - stamp.astimezone(now.tzinfo)).total_seconds() / 3600
        except (TypeError, ValueError):
            continue
        if age_hours > 24:
            problems.append(
                f"{order.get('symbol', '?')}: non-protective order "
                f"{order.get('id', '?')} is {age_hours:.1f}h old"
            )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=list(PROFILES), default="base")
    parser.add_argument("--max-age-hours", type=float, default=72.0)
    parser.add_argument(
        "--allow-pristine",
        action="store_true",
        help="allow a completely unused account/journal to bootstrap an upgrade",
    )
    args = parser.parse_args()

    _, env_suffix, state_suffix = PROFILES[args.profile]
    db_path = REPO_ROOT / "state" / f"paper{state_suffix}.db"

    load_env()
    trader = Trader(
        key=os.environ.get(f"ALPACA_API_KEY{env_suffix}"),
        secret=os.environ.get(f"ALPACA_API_SECRET{env_suffix}"),
    )
    account = trader.get_account()
    positions = trader.get_positions()
    orders = trader.open_orders()

    cfg_file, _, _ = PROFILES[args.profile]
    exempt_sleeves = load_config(REPO_ROOT / cfg_file).risk.stop_exempt_sleeves

    last_snapshot = None
    fallback_stops: set[str] = set()
    unstopped_symbols: set[str] = set()
    journal_is_pristine = True
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        unstopped_symbols = unstopped_from_journal(conn, exempt_sleeves)
        row = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()
        if row and row[0]:
            last_snapshot = dt.datetime.fromisoformat(row[0])
        fallback_stops = {
            str(row[0]) for row in conn.execute("SELECT symbol FROM stops").fetchall()
        }
        journal_is_pristine = all(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in ("snapshots", "orders", "stops")
        )

    now = dt.datetime.now(ET)
    problems = assess_health(
        account_status=str(account.get("status", "")),
        positions=positions,
        open_orders=orders,
        fallback_stops=fallback_stops,
        last_snapshot=last_snapshot,
        now=now,
        max_age_hours=args.max_age_hours,
        allow_pristine=args.allow_pristine,
        journal_is_pristine=journal_is_pristine,
        unstopped_symbols=unstopped_symbols,
    )

    # A second daily check on the options structure(s) scripts.options_daily
    # opens/closes (2x-lab only — base never carries any) — reuses the same
    # pure reconciliation function that script calls itself, so an
    # assignment-detection anomaly is caught twice a day, not once, for
    # free. options_2x.db may not exist yet on a profile that has never run
    # scripts.options_daily; that is healthy, not a problem.
    if args.profile == "2x":
        open_structures = open_option_structures(OPTIONS_DB)
        # equity_explained_qty distinguishes a genuinely unexplained equity
        # position (a possible assignment) from an underlying — SPY, in
        # this repo's only live experiment — that's ALSO an ordinary
        # equity_core/trend holding, which would otherwise flag on every
        # single day the options structure is open. Computed from the same
        # `conn` opened above, when it exists; a pristine profile (no
        # db_path yet) has no orders to explain, so falls back to the
        # original, stricter "everything is unexplained" behavior via the
        # empty dict default.
        equity_explained_qty = {
            s["underlying"]: equity_qty_explained_by_orders(conn, s["underlying"])
            for s in open_structures
        } if db_path.exists() else {}
        problems += reconcile_option_structures(
            positions, open_structures, equity_explained_qty=equity_explained_qty
        )

    # Persisted for the read-only dashboard (dashboard/), which must never
    # call Alpaca itself — this is the one place that result reaches disk.
    # Written unconditionally (healthy or not) so the dashboard can show
    # either state, not just failures.
    health_status_path = REPO_ROOT / "state" / f"health_status{state_suffix}.json"
    health_status_path.write_text(json.dumps({
        "ts": now.isoformat(),
        "healthy": not problems,
        "problems": problems,
        "equity": account.get("equity"),
        "positions": len(positions),
        "open_orders": len(orders),
    }))

    print(
        f"profile={args.profile} equity={account.get('equity')} "
        f"positions={len(positions)} open_orders={len(orders)}"
    )
    if problems:
        for problem in problems:
            print(f"CRITICAL: {problem}")
        raise SystemExit(1)
    if args.allow_pristine and journal_is_pristine and not positions and not orders:
        print("HEALTHY (pristine profile; first live run must create a snapshot)")
    else:
        print("HEALTHY")


if __name__ == "__main__":
    main()
